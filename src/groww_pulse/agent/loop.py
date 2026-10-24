"""The bounded agent loop (architecture.md §4.10): the Groq model picks the next tool, the tools do the work.

Guardrails: at most `max_steps` model calls, at most `max_repeat` identical calls (same tool, same arguments) in a
row, write tools gated on
validation (in AgentTools), and the final status is derived from tool results, never from the model's text.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Literal

import groq

from groww_pulse.agent.tools import AgentTools
from groww_pulse.llm import LLMContractError
from groww_pulse.prompts import load_prompt
from groww_pulse.runner import RunContext, read_run_json, write_run_json
from groww_pulse.stages import db_path
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import purge_snapshots

PUBLISH_TOOLS = ("publish_doc", "upsert_gmail_draft")


@dataclass(frozen=True)
class AgentResult:
    week: str
    status: Literal["ok", "partial", "failed"]
    final_message: str
    doc_url: str | None
    draft_id: str | None
    steps: int
    trace: list[dict] = field(default_factory=list)
    reason: str | None = None  # agent_step_limit, agent_repeat_limit or llm_unavailable when a guardrail stopped it


def default_goal(week: str) -> str:
    return f"Produce and publish the Groww pulse for {week}. Check the run report first and reuse completed stages."


async def run_agent(ctx: RunContext, llm, tools: AgentTools, goal: str | None = None) -> AgentResult:
    cfg = ctx.settings.agent
    messages = [{"role": "system", "content": load_prompt("agent_system")},
                {"role": "user", "content": goal or default_goal(ctx.week)}]
    trace: list[dict] = []
    published: dict[str, dict] = {}  # publish tool -> its last successful result
    steps, reason, final_message = 0, None, ""
    last_call, streak = None, 0
    try:
        while reason is None:
            if steps >= cfg.max_steps:
                reason = "agent_step_limit"
                break
            try:
                reply = llm.chat_with_tools(messages, tools.schemas(), reasoning_effort=cfg.reasoning_effort)
            except (groq.GroqError, LLMContractError) as e:
                reason = "llm_unavailable"
                final_message = (f"The Groq model is unavailable ({type(e).__name__}), so the agent stopped. "
                                 f"Run the deterministic pipeline instead: pulse run --week {ctx.week}")
                break
            steps += 1
            calls = reply.get("tool_calls") or []
            if not calls:
                final_message = reply.get("content") or ""
                break
            messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                # A loop is the same call again; run_stage over four different stages is progress, not a loop.
                key = (name, c["function"].get("arguments") or "")
                streak = streak + 1 if key == last_call else 1
                last_call = key
                if streak > cfg.max_repeat:
                    reason = "agent_repeat_limit"
                    break
                start = time.perf_counter()
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args, result = None, {"ok": False, "error": "arguments are not valid JSON"}
                else:
                    result = await tools.dispatch(name, args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
                trace.append({"step": steps, "tool": name, "args": args, "ok": bool(result.get("ok")),
                              "ms": round((time.perf_counter() - start) * 1000)})
                if name in PUBLISH_TOOLS and result.get("ok"):
                    published[name] = result
    finally:
        purge_snapshots(ctx.settings.data_dir, ctx.today())

    if reason or not published:
        status = "failed"
    else:
        status = "ok" if len(published) == len(PUBLISH_TOOLS) else "partial"
    if reason and not final_message:
        final_message = f"Stopped by a guardrail ({reason}). Check run.json.agent_trace, or run: pulse run --week {ctx.week}"
    result = AgentResult(ctx.week, status, final_message, published.get("publish_doc", {}).get("doc_url"),
                         published.get("upsert_gmail_draft", {}).get("draft_id"), steps, trace, reason)
    run = read_run_json(ctx)
    run.update(agent_status=status, agent_reason=reason, agent_trace=trace)
    write_run_json(ctx, run)
    with Database(db_path(ctx.settings)) as db:
        db.record_run(ctx.week, status)
    return result
