import json
import re
from dataclasses import asdict
from datetime import date

import pytest
from fakes.fake_mcp import fake_session_factory, make_fake_server
from test_stages import PHONE, USER, _raw, labeler

from groww_pulse.config import load_settings
from groww_pulse.llm import FakeLLM
from groww_pulse.privacy.scrubber import find_pattern_pii
from groww_pulse.publish.mcp_client import McpGateway
from groww_pulse.runner import STAGES, RunContext, RunDeps, StageResult, run_pipeline, run_stage
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import save_snapshot
from groww_pulse.themes.embed import HashEmbedder

WEEK = "2026-W43"
TODAY = date(2026, 10, 26)


class ListSource:
    name = "play_store"

    def __init__(self, items=None, error: Exception | None = None):
        self.items, self.error, self.fetches = items, error, 0

    def fetch(self, since, until):
        self.fetches += 1
        if self.error:
            raise self.error
        return list(self.items)


def writer(system, user, schema):
    """A valid draft: one summary per theme in the context, 3 actions spread over those themes."""
    ctx = json.loads(user.split("<data>")[1].split("</data>")[0])
    names = [t["name"] for t in ctx["themes"]]
    return {"theme_summaries": [f"Users report repeated problems with {n.lower()} this month." for n in names],
            "actions": [f"{names[i % len(names)]}: investigate the top complaints and track weekly volume."
                        for i in range(3)]}


def llm() -> FakeLLM:
    """Labels clusters during themes, writes the draft during pulse; the number of calls varies with data."""
    return FakeLLM([lambda s, u, schema: writer(s, u, schema) if "<data>" in u else labeler(s, u, schema)] * 20)


async def no_sleep(_: float) -> None:
    pass


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    return load_settings().model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def server():
    return make_fake_server()


def gateways(settings, server):
    f = fake_session_factory(server)
    return (McpGateway(settings.mcp.docs, "docs", session_factory=f, sleep=no_sleep),
            McpGateway(settings.mcp.gmail, "gmail", session_factory=f, sleep=no_sleep))


def run(settings, server, source=None, **kw):
    kw.setdefault("today", TODAY)
    return run_pipeline(settings, WEEK, source=source or ListSource(_raw()), llm=kw.pop("llm", None) or llm(),
                        embedder=HashEmbedder(), gateways=kw.pop("gateways", None) or gateways(settings, server),
                        sleep=lambda s: None, **kw)


def run_json(settings) -> dict:
    return json.loads((settings.data_dir / "runs" / WEEK / "run.json").read_text(encoding="utf-8"))


def test_stage_names():
    assert STAGES == ("ingest", "normalize", "themes", "pulse", "publish")


def test_dry_run_writes_pulse_and_skips_publish(settings, server):
    result = run(settings, server, dry_run=True)
    assert result.status == "dry_run" and result.error is None
    assert result.pulse_path.read_text(encoding="utf-8").startswith("# Groww — Weekly Review Pulse")
    assert result.doc_url is None and result.draft_id is None
    assert server.docs == {} and server.drafts == {}
    assert run_json(settings)["status"] == "dry_run"


def test_full_run_publishes_and_records_run(settings, server):
    save_snapshot(settings.data_dir, "play_store", date(2026, 9, 1), [])  # older than 14 days -> purged
    result = run(settings, server)
    assert result.status == "ok", result.error
    assert result.doc_url == "https://docs.example.com/d/doc1" and result.draft_id == "draft1"
    md = result.pulse_path.read_text(encoding="utf-8")
    assert server.docs["doc1"]["content"] == md
    assert server.drafts["draft1"]["body"] == f"Doc: {result.doc_url}\n\n{md}"
    assert server.sent == []

    rj = run_json(settings)
    assert (rj["status"], rj["doc_url"], rj["draft_id"]) == ("ok", result.doc_url, "draft1")
    assert rj["validation"] == {"ok": True, "failures": []} and rj["model"] == "fake"
    assert rj["reasoning_effort"] == settings.reasoning_effort and len(rj["prompts"]) == 3
    assert {c["server"] for c in rj["mcp_calls"]} == {"docs", "gmail"}
    assert rj["counts"]["reviews"] == 41
    with Database(settings.data_dir / "reviews.sqlite") as db:
        assert db.get_run(WEEK)["status"] == "ok" and db.get_run(WEEK)["draft_id"] == "draft1"
    snaps = sorted(p.name for p in (settings.data_dir / "raw" / "play_store").iterdir())
    assert snaps == ["2026-10-26.json"]


def test_rerun_same_week_reuses_doc_and_draft(settings, server):
    first = run(settings, server)
    second = run(settings, server)
    assert (first.doc_url, first.draft_id) == (second.doc_url, second.draft_id)
    assert len(server.docs) == 1 and len(server.drafts) == 1


def test_resume_from_pulse_stage_skips_ingest(settings, server):
    run(settings, server, dry_run=True)
    exploding = ListSource(error=AssertionError("fetch must not be called"))
    result = run(settings, server, source=exploding, from_stage="pulse")
    assert result.status == "ok", result.error
    assert exploding.fetches == 0


def test_source_failure_falls_back_to_snapshot(settings, server):
    save_snapshot(settings.data_dir, "play_store", date(2026, 10, 24), _raw())
    down = ListSource(error=ConnectionError("play store down"))
    sleeps = []
    result = run_pipeline(settings, WEEK, dry_run=True, source=down, llm=llm(), embedder=HashEmbedder(),
                          today=TODAY, sleep=sleeps.append)
    assert down.fetches == 3 and sleeps == [1, 2]
    assert result.status == "dry_run" and "source_fallback" in result.flags


def test_source_failure_without_snapshot_fails(settings, server):
    result = run(settings, server, source=ListSource(error=ConnectionError("down")))
    assert result.status == "failed" and result.error.startswith("no_reviews")
    assert server.docs == {} and server.drafts == {}
    assert run_json(settings)["status"] == "failed"


def test_docs_failure_yields_partial_with_inline_draft(settings, server):
    server.fail_next["create_document"] = RuntimeError("docs down")  # create fails and find-again finds nothing
    result = run(settings, server)
    assert result.status == "partial" and "docs_failed" in result.flags
    assert result.doc_url is None and server.docs == {}
    md = result.pulse_path.read_text(encoding="utf-8")
    assert server.drafts[result.draft_id]["body"] == md  # pulse inline, no Doc line
    assert run_json(settings)["status"] == "partial"


def test_gmail_failure_yields_partial_with_doc_url(settings, server):
    server.fail_next["create_draft"] = RuntimeError("gmail down")
    result = run(settings, server)
    assert result.status == "partial" and "gmail_failed" in result.flags
    assert result.doc_url == "https://docs.example.com/d/doc1" and result.draft_id is None
    assert run_json(settings)["doc_url"] == result.doc_url


def test_misconfigured_mcp_fails_before_any_write(settings, server):
    from groww_pulse.config import ToolSpec

    bad_docs = settings.mcp.docs.model_copy(
        update={"tools": {**settings.mcp.docs.tools, "create_doc": ToolSpec(name="createDoc")}})
    f = fake_session_factory(server)
    gws = (McpGateway(bad_docs, "docs", session_factory=f), McpGateway(settings.mcp.gmail, "gmail", session_factory=f))
    result = run(settings, server, gateways=gws)
    assert result.status == "failed" and "createDoc" in result.error and "create_document" in result.error
    assert server.docs == {} and server.drafts == {}


def test_run_json_contains_no_pii(settings, server):
    run(settings, server)
    files = [p for p in (settings.data_dir / "runs" / WEEK).iterdir() if p.is_file() and p.name != ".lock"]
    assert {p.name for p in files} == {"themes.json", "quotes.json", "pulse.md", "run.json"}
    for p in files:
        text = p.read_text(encoding="utf-8")
        # Salted review hashes, prompt sha256s and float shares are not PII, but their digit runs look like ids
        assert find_pattern_pii(re.sub(r"\b[0-9a-f]{64}\b|\d+\.\d+", "", text)) == [], p.name
        assert USER not in text and "Priya" not in text and PHONE not in text and "gp:" not in text
    quotes = json.loads((settings.data_dir / "runs" / WEEK / "quotes.json").read_text(encoding="utf-8"))["quotes"]
    assert all(len(q["review_id"]) == 64 for q in quotes)  # salted hash, never the source id


def test_publish_stage_refuses_unvalidated_pulse(settings, server):
    run(settings, server, dry_run=True)
    out = settings.data_dir / "runs" / WEEK
    rj = run_json(settings)
    rj["validation"] = {"ok": False, "failures": ["word_count: 300 > 250"]}
    (out / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    ctx = RunContext(settings, WEEK, RunDeps(gateways=gateways(settings, server), today=TODAY))
    result = run_stage(ctx, "publish")
    assert not result.ok and "validat" in result.error
    assert server.docs == {} and server.drafts == {}


def test_publish_stage_refuses_missing_pulse(settings, server):
    ctx = RunContext(settings, WEEK, RunDeps(gateways=gateways(settings, server), today=TODAY))
    result = run_stage(ctx, "publish")
    assert not result.ok and server.docs == {}


def test_stage_result_contains_no_review_text(settings, server):
    deps = RunDeps(source=ListSource(_raw()), llm=llm(), embedder=HashEmbedder(),
                   gateways=gateways(settings, server), today=TODAY, sleep=lambda s: None)
    ctx = RunContext(settings, WEEK, deps)
    results = [run_stage(ctx, s) for s in STAGES]
    assert all(isinstance(r, StageResult) and r.ok for r in results), [r.error for r in results]
    dumped = json.dumps([asdict(r) for r in results])
    with Database(settings.data_dir / "reviews.sqlite") as db:
        texts = [r.text for r in db.reviews_in_window(date(2026, 8, 1), date(2026, 10, 25))]
    assert texts and not any(t[:30] in dumped for t in texts)
    assert all(isinstance(v, int) for r in results for v in r.counts.values())


def test_low_volume_widens_window_and_flags(settings, server):
    """Review Focus #2: 8 reviews -> 12-week window, flagged low_volume, a reduced pulse rather than a failure."""
    raw = [r for r in _raw() if r.source_review_id.split(":")[-1] in ("0", "1")][:8]
    result = run(settings, server, source=ListSource(raw), dry_run=True)
    assert result.status == "dry_run", result.error
    assert "low_volume" in result.flags and "window_widened" in result.flags
    meta = json.loads((settings.data_dir / "runs" / WEEK / "themes.json").read_text(encoding="utf-8"))
    assert meta["window"]["start"] == "2026-08-03" and meta["total_reviews"] == 8
    assert "few_themes" in result.flags  # 8 reviews stay one cluster
    md = result.pulse_path.read_text(encoding="utf-8")
    assert md.count('" (★') == 3
    assert set(run_json(settings)["flags"]) >= {"low_volume", "window_widened", "few_themes"}


def test_llm_unavailable_fails_without_publishing(settings, server):
    import groq

    result = run(settings, server, llm=FakeLLM([groq.GroqError("The api_key client option must be set")]))
    assert result.status == "failed" and result.error.startswith("themes: llm_unavailable")
    assert "api_key" not in result.error  # error kind only
    assert server.docs == {} and server.drafts == {}


def test_republish_after_partial_clears_failure_flags(settings, server):
    server.fail_next["create_document"] = RuntimeError("docs down")
    assert run(settings, server).status == "partial"
    result = run(settings, server, from_stage="publish")
    assert result.status == "ok" and result.doc_url
    rj = run_json(settings)
    assert rj["status"] == "ok" and not any(f.endswith("_failed") for f in rj["flags"])
    assert len(server.drafts) == 1  # the partial run's inline draft is updated with the Doc link
