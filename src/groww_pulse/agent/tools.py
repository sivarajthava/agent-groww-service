"""Tools the agent model may call (architecture.md §4.10). Each takes only `week` (and a stage name).

The model decides when to run a step; these functions decide what happens. Write tools publish the validated
pulse.md from disk and refuse until validation has passed. Results hold counts, flags, statuses and URLs, never
review text, except `get_pulse`, which returns the already validated and scrubbed pulse. Raw MCP tools are
never exposed: publishing goes through the same publishers and McpGateway as the runner.
"""

from collections.abc import Callable
from dataclasses import asdict

import jsonschema

from groww_pulse.publish.docs import publish_doc
from groww_pulse.publish.gmail import upsert_draft
from groww_pulse.publish.mcp_client import McpGateway
from groww_pulse.pulse.render import render_html
from groww_pulse.runner import RunContext, read_run_json, run_dir, run_stage, write_run_json
from groww_pulse.stages import db_path
from groww_pulse.storage.db import Database

AGENT_STAGES = ["ingest", "normalize", "themes", "pulse"]  # publish only through the gated write tools
WEEK = {"type": "string", "pattern": r"^\d{4}-W\d{2}$", "description": "ISO week, e.g. 2026-W43"}
NOT_VALIDATED = {"ok": False, "error": "pulse not validated"}

TOOLS = {
    "run_stage": ("Run one pipeline stage for the week and return its counts, flags and error. Stages: ingest "
                  "(download reviews), normalize (scrub and store), themes (themes and quotes), pulse (write and "
                  "validate pulse.md).",
                  {"week": WEEK, "stage": {"type": "string", "enum": AGENT_STAGES}}),
    "get_run_report": ("Read the week's run report: status, flags, validation result, doc_url, draft_id, counts, "
                       "and which stage outputs exist.", {"week": WEEK}),
    "get_pulse": ("Read the week's validated pulse (Markdown).", {"week": WEEK}),
    "publish_doc": ("Create or update the week's Google Doc from the validated pulse. Refused until validation "
                    "passed.", {"week": WEEK}),
    "upsert_gmail_draft": ("Create or update the week's Gmail draft (never sent) with the validated pulse and the "
                           "Doc link. Refused until validation passed.", {"week": WEEK}),
}


def _schema(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


class AgentTools:
    def __init__(self, ctx: RunContext, gateway_factory: Callable[[str], McpGateway]):
        self.ctx, self.gateway_factory = ctx, gateway_factory

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {"name": name, "description": desc, "parameters": _schema(props)}}
                for name, (desc, props) in TOOLS.items()]

    async def dispatch(self, name: str, args: dict | None) -> dict:
        """Validate and run one tool call. Never raises: every problem comes back as {"ok": False, "error": ...}."""
        if name not in TOOLS:
            return {"ok": False, "error": "unknown tool"}
        try:
            jsonschema.validate(args, _schema(TOOLS[name][1]))
        except jsonschema.ValidationError as e:
            return {"ok": False, "error": f"invalid arguments: {e.message}"}
        if args["week"] != self.ctx.week:
            return {"ok": False, "error": f"week must be {self.ctx.week}"}
        try:
            return await getattr(self, f"_{name}")(args)
        except Exception as e:  # a tool failure is a result the model can react to, not a crash of the loop
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    async def _run_stage(self, args: dict) -> dict:
        return asdict(run_stage(self.ctx, args["stage"]))

    async def _get_run_report(self, args: dict) -> dict:
        out = run_dir(self.ctx.settings, self.ctx.week)
        run = read_run_json(self.ctx)
        return {
            "ok": True,
            "status": run.get("status"),
            "flags": run.get("flags", []),
            "validation": run.get("validation"),
            "doc_url": run.get("doc_url"),
            "draft_id": run.get("draft_id"),
            "counts": run.get("counts", {}),
            "error": run.get("error"),
            "outputs": {name: (out / f"{name}.{ext}").exists()
                        for name, ext in (("themes", "json"), ("quotes", "json"), ("pulse", "md"))},
        }

    async def _get_pulse(self, args: dict) -> dict:
        path = run_dir(self.ctx.settings, self.ctx.week) / "pulse.md"
        if not path.exists():
            return {"ok": False, "error": "no pulse.md for this week; run the pulse stage"}
        return {"ok": True, "markdown": path.read_text(encoding="utf-8")}

    def _validated_pulse(self) -> str | None:
        path = run_dir(self.ctx.settings, self.ctx.week) / "pulse.md"
        if not read_run_json(self.ctx).get("validation", {}).get("ok") or not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    async def _publish(self, server: str, publish) -> dict:
        """Open one gateway, publish, and record the result and the MCP audit log in run.json and SQLite."""
        gw = self.gateway_factory(server)
        try:
            async with gw:
                result = await publish(gw)
        finally:
            run = read_run_json(self.ctx)
            run["mcp_calls"] = run.get("mcp_calls", []) + gw.calls
            write_run_json(self.ctx, run)
        run = read_run_json(self.ctx)
        run.update(result)
        write_run_json(self.ctx, run)
        with Database(db_path(self.ctx.settings)) as db:
            db.record_run(self.ctx.week, "publishing", doc_url=result.get("doc_url"), draft_id=result.get("draft_id"))
        return {"ok": True, **result}

    async def _publish_doc(self, args: dict) -> dict:
        markdown = self._validated_pulse()
        if markdown is None:
            return NOT_VALIDATED

        async def publish(gw):
            _, doc_url = await publish_doc(gw, self.ctx.week, markdown)
            return {"doc_url": doc_url}

        return await self._publish("docs", publish)

    async def _upsert_gmail_draft(self, args: dict) -> dict:
        markdown = self._validated_pulse()
        if markdown is None:
            return NOT_VALIDATED
        doc_url = read_run_json(self.ctx).get("doc_url")
        settings = self.ctx.settings

        async def publish(gw):
            draft_id = await upsert_draft(gw, self.ctx.week, settings.recipient, markdown, render_html(markdown),
                                          doc_url)
            return {"draft_id": draft_id}

        return await self._publish("gmail", publish)
