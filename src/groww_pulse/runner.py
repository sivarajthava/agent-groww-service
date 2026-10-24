"""The pipeline runner: ingest → normalize → themes → pulse → publish, resumable per stage (architecture.md §7).

Each stage reads its input from SQLite or data/runs/<week>/ and writes its output there, so `run_pipeline` can
start at any stage and the agent tools can run one stage at a time through `run_stage`. Stage results carry counts
and flags only, never review text.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from groww_pulse.config import Settings
from groww_pulse.ingest.base import ReviewSource
from groww_pulse.llm import LLMClient, LLMContractError
from groww_pulse.publish.docs import publish_doc
from groww_pulse.publish.gmail import upsert_draft
from groww_pulse.publish.mcp_client import CapabilityMissing, ConfigError, McpGateway, ToolNotAllowed
from groww_pulse.pulse.render import render_html
from groww_pulse.pulse.validate import ValidationFailed
from groww_pulse.stages import SOURCE, StageError, db_path, normalize_stage, pulse_stage, run_dir, themes_stage
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import load_latest_snapshot, purge_snapshots, save_snapshot
from groww_pulse.themes.quotes import NotEnoughQuotes
from groww_pulse.weeks import week_window

STAGES = ("ingest", "normalize", "themes", "pulse", "publish")
INGEST_ATTEMPTS = 3
# Errors that mean the MCP setup is wrong: the run stops before any write instead of publishing half.
MCP_CONFIG_ERRORS = (CapabilityMissing, ConfigError, ToolNotAllowed)


@dataclass(frozen=True)
class StageResult:
    stage: str
    ok: bool
    counts: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class RunResult:
    week: str
    status: Literal["ok", "partial", "failed", "dry_run"]
    pulse_path: Path | None
    doc_url: str | None
    draft_id: str | None
    flags: list[str]
    error: str | None


@dataclass
class RunDeps:
    """Injected dependencies. Anything left None is built from settings the first time a stage needs it."""

    source: ReviewSource | None = None
    llm: LLMClient | None = None
    embedder: object | None = None
    gateways: tuple[McpGateway, McpGateway] | None = None
    today: date | None = None
    sleep: Callable[[float], None] = time.sleep


@dataclass
class RunContext:
    settings: Settings
    week: str
    deps: RunDeps = field(default_factory=RunDeps)

    def source(self) -> ReviewSource:
        if self.deps.source is None:
            from groww_pulse.ingest.play_store import PlayStorePublicSource

            s = self.settings
            self.deps.source = PlayStorePublicSource(s.app_id, s.lang, s.country)
        return self.deps.source

    def llm(self) -> LLMClient:
        if self.deps.llm is None:
            from groww_pulse.llm import GroqClient

            s = self.settings
            self.deps.llm = GroqClient(s.model, s.fallback_model, s.reasoning_effort, s.temperature)
        return self.deps.llm

    def embedder(self):
        if self.deps.embedder is None:
            from groww_pulse.themes.embed import Embedder

            self.deps.embedder = Embedder()
        return self.deps.embedder

    def gateways(self) -> tuple[McpGateway, McpGateway]:
        if self.deps.gateways is None:
            m = self.settings.mcp
            self.deps.gateways = (McpGateway(m.docs, "docs", m.timeout_s), McpGateway(m.gmail, "gmail", m.timeout_s))
        return self.deps.gateways

    def today(self) -> date:
        return self.deps.today or date.today()


def _ints(d: dict) -> dict[str, int]:
    return {k: v for k, v in d.items() if isinstance(v, int) and not isinstance(v, bool)}


def _read_run_json(ctx: RunContext) -> dict:
    path = run_dir(ctx.settings, ctx.week) / "run.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"week": ctx.week}


def _write_run_json(ctx: RunContext, data: dict) -> None:
    path = run_dir(ctx.settings, ctx.week) / "run.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ingest(ctx: RunContext) -> StageResult:
    """Fetch the widest window; retry with backoff, then fall back to the latest raw snapshot."""
    s = ctx.settings
    since, until = week_window(ctx.week, s.max_window_weeks)
    source, error = ctx.source(), None
    for attempt in range(INGEST_ATTEMPTS):
        try:
            items = source.fetch(since, until)
        except Exception as e:  # network, parsing, rate limit: all mean "try again, then use the snapshot"
            error = f"{type(e).__name__}: {e}"[:200]
            if attempt < INGEST_ATTEMPTS - 1:
                ctx.deps.sleep(2**attempt)  # 1 s, then 2 s
            continue
        if items:
            save_snapshot(s.data_dir, source.name, ctx.today(), items)
            return StageResult("ingest", True, {"fetched": len(items)})
        error = "source returned no reviews"
        break
    snapshot = load_latest_snapshot(s.data_dir, SOURCE)
    if not snapshot:
        return StageResult("ingest", False, error=f"ingest: {error}; no raw snapshot to fall back to")
    return StageResult("ingest", True, {"fetched": 0, "snapshot": len(snapshot)}, ["source_fallback"])


async def _publish_async(ctx: RunContext, markdown: str) -> tuple[str | None, str | None, list[str], list[dict]]:
    """Docs, then Gmail. A server that is down only loses its own half; a wrong MCP config raises before writes."""
    docs_gw, gmail_gw = ctx.gateways()
    s, flags = ctx.settings, []
    doc_url = draft_id = None
    entered: list[McpGateway] = []
    try:
        for gw in (docs_gw, gmail_gw):  # connect and check both capability maps before writing anything
            try:
                await gw.__aenter__()
                entered.append(gw)
            except MCP_CONFIG_ERRORS:
                raise
            except Exception:
                flags.append(f"{gw.name}_failed")
        if docs_gw in entered:
            try:
                _, doc_url = await publish_doc(docs_gw, ctx.week, markdown)
            except MCP_CONFIG_ERRORS:
                raise
            except Exception:
                flags.append("docs_failed")
        if gmail_gw in entered:
            try:
                draft_id = await upsert_draft(gmail_gw, ctx.week, s.recipient, markdown, render_html(markdown),
                                              doc_url)
            except MCP_CONFIG_ERRORS:
                raise
            except Exception:
                flags.append("gmail_failed")
    finally:
        for gw in reversed(entered):
            await gw.__aexit__(None, None, None)
    return doc_url, draft_id, flags, docs_gw.calls + gmail_gw.calls


def _publish(ctx: RunContext) -> StageResult:
    """Publish the validated pulse.md from disk. Refuses unless run.json says validation passed."""
    out = run_dir(ctx.settings, ctx.week)
    run = _read_run_json(ctx)
    pulse_md = out / "pulse.md"
    if not run.get("validation", {}).get("ok") or not pulse_md.exists():
        return StageResult("publish", False, error="publish: no validated pulse.md for this week; run the pulse stage")
    markdown = pulse_md.read_text(encoding="utf-8")
    try:
        doc_url, draft_id, flags, calls = asyncio.run(_publish_async(ctx, markdown))
    except MCP_CONFIG_ERRORS as e:
        gws = ctx.deps.gateways or ()
        run["mcp_calls"] = [c for gw in gws for c in gw.calls]
        _write_run_json(ctx, run)
        return StageResult("publish", False, error=f"publish: {type(e).__name__}: {e}")
    run.update(doc_url=doc_url, draft_id=draft_id, mcp_calls=calls)
    _write_run_json(ctx, run)
    with Database(db_path(ctx.settings)) as db:
        db.record_run(ctx.week, "publishing", doc_url=doc_url, draft_id=draft_id)
    counts = {"doc": int(doc_url is not None), "draft": int(draft_id is not None)}
    if doc_url is None and draft_id is None:
        return StageResult("publish", False, counts, flags, "publish: neither the Doc nor the draft was written")
    return StageResult("publish", True, counts, flags)


def run_stage(ctx: RunContext, stage: str) -> StageResult:
    """Run one stage. Expected failures come back as ok=False with a short error, never review text."""
    if stage not in STAGES:
        return StageResult(stage, False, error=f"unknown stage {stage!r}; stages are {', '.join(STAGES)}")
    if stage == "ingest":
        return _ingest(ctx)
    if stage == "publish":
        return _publish(ctx)
    s = ctx.settings
    try:
        with Database(db_path(s)) as db:
            if stage == "normalize":
                return StageResult(stage, True, _ints(normalize_stage(s, ctx.week, db)))
            if stage == "themes":
                res = themes_stage(s, ctx.week, db, ctx.embedder(), ctx.llm())
                return StageResult(stage, True, _ints(res), list(res["flags"]))
            res = pulse_stage(s, ctx.week, db, ctx.llm())
            return StageResult(stage, True, _ints(res))
    except (StageError, NotEnoughQuotes, ValidationFailed, LLMContractError) as e:
        return StageResult(stage, False, error=f"{stage}: {e}")


def run_pipeline(
    settings: Settings,
    week: str,
    *,
    dry_run: bool = False,
    from_stage: str = "ingest",
    source: ReviewSource | None = None,
    llm: LLMClient | None = None,
    embedder=None,
    gateways: tuple[McpGateway, McpGateway] | None = None,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    if from_stage not in STAGES:
        raise ValueError(f"unknown stage {from_stage!r}; stages are {', '.join(STAGES)}")
    ctx = RunContext(settings, week, RunDeps(source, llm, embedder, gateways, today, sleep))
    stages = [st for st in STAGES[STAGES.index(from_stage):] if not (dry_run and st == "publish")]
    flags: list[str] = []
    error = None
    try:
        for stage in stages:
            result = run_stage(ctx, stage)
            flags += [f for f in result.flags if f not in flags]
            if not result.ok:
                error = result.error
                break
    finally:
        purge_snapshots(settings.data_dir, ctx.today())

    if error:
        status = "failed"
    elif dry_run:
        status = "dry_run"
    elif any(f.endswith("_failed") for f in flags):
        status = "partial"
    else:
        status = "ok" if "publish" in stages else "dry_run"
    run = _read_run_json(ctx)
    run.update(status=status, flags=sorted(set(run.get("flags", [])) | set(flags)), error=error)
    _write_run_json(ctx, run)
    with Database(db_path(settings)) as db:
        db.record_run(week, status)
    pulse_md = run_dir(settings, week) / "pulse.md"
    return RunResult(week, status, pulse_md if pulse_md.exists() else None, run.get("doc_url"),
                     run.get("draft_id"), flags, error)
