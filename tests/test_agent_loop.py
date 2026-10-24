import json
from itertools import count

import groq
import pytest
from test_agent_tools import make_tools, set_validation
from test_runner import WEEK, ListSource, llm, run_json
from test_stages import _raw

from groww_pulse.agent.loop import AgentResult, default_goal, run_agent
from groww_pulse.config import load_settings
from groww_pulse.llm import FakeLLM
from groww_pulse.runner import run_pipeline
from groww_pulse.themes.embed import HashEmbedder
from fakes.fake_mcp import make_fake_server

_ids = count(1)


def call(name: str, **args) -> dict:
    """An assistant message asking for one tool call, as GroqClient.chat_with_tools returns it."""
    args.setdefault("week", WEEK)
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{next(_ids)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def final(text: str) -> dict:
    return {"role": "assistant", "content": text}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    return load_settings().model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def server():
    return make_fake_server()


@pytest.fixture
def validated(settings):
    result = run_pipeline(settings, WEEK, dry_run=True, source=ListSource(_raw()), llm=llm(),
                          embedder=HashEmbedder(), sleep=lambda s: None)
    assert result.status == "dry_run", result.error


async def agent(settings, server, script) -> tuple[AgentResult, FakeLLM]:
    tools = make_tools(settings, server)
    model = FakeLLM(script)
    return await run_agent(tools.ctx, model, tools), model


async def test_happy_path_reaches_ok(settings, server):
    script = [call("get_run_report")] + [call("run_stage", stage=s) for s in ("ingest", "normalize", "themes", "pulse")]
    script += [call("publish_doc"), call("upsert_gmail_draft"), final("Published.\nDoc and draft updated.\nok")]
    result, model = await agent(settings, server, script)
    assert result.status == "ok" and result.reason is None, result
    assert result.doc_url == "https://docs.example.com/d/doc1" and result.draft_id == "draft1"
    assert result.final_message.startswith("Published.") and result.steps == 8
    assert [t["tool"] for t in result.trace] == ["get_run_report", "run_stage", "run_stage", "run_stage", "run_stage",
                                                 "publish_doc", "upsert_gmail_draft"]
    assert all(t["ok"] for t in result.trace)
    first = model.chat_calls[0]
    assert first[0]["role"] == "system" and "get_run_report" in first[0]["content"]
    assert first[1] == {"role": "user", "content": default_goal(WEEK)}
    # every tool result went back to the model as a role=tool message tied to its call id
    last = model.chat_calls[-1]
    tool_msgs = [m for m in last if m["role"] == "tool"]
    assert len(tool_msgs) == 7 and json.loads(tool_msgs[-1]["content"]) == {"ok": True, "draft_id": "draft1"}
    assert server.sent == []


async def test_step_limit_stops_loop(settings, server, validated):
    """Review Focus #7: a model that never stops is cut off at max_steps; nothing more is published."""
    script = [call("get_run_report") if i % 2 else call("get_pulse") for i in range(30)]
    result, _ = await agent(settings, server, script)
    assert (result.status, result.reason) == ("failed", "agent_step_limit")
    assert result.steps == settings.agent.max_steps == 12
    assert server.docs == {}


async def test_repeat_limit_stops_loop(settings, server, validated):
    """Review Focus #7: the same tool more than max_repeat times in a row stops the loop."""
    result, _ = await agent(settings, server, [call("get_pulse")] * 4 + [final("done")])
    assert (result.status, result.reason) == ("failed", "agent_repeat_limit")
    assert [t["tool"] for t in result.trace] == ["get_pulse"] * 3  # the 4th call is never run


async def test_repeat_limit_counts_identical_calls_only(settings, server, validated):
    """Four different stages in a row are progress; the fourth identical stage call is a loop."""
    stages = [call("run_stage", stage=s) for s in ("ingest", "normalize", "themes", "pulse")]
    result, _ = await agent(settings, server, stages + [final("done")])
    assert result.reason is None and len(result.trace) == 4
    result, _ = await agent(settings, server, [call("run_stage", stage="pulse")] * 4 + [final("done")])
    assert result.reason == "agent_repeat_limit" and len(result.trace) == 3


async def test_status_from_tool_results_not_model_text(settings, server, validated):
    set_validation(settings, False)
    script = [call("publish_doc"), call("upsert_gmail_draft"), final("Published! Everything is live.")]
    result, _ = await agent(settings, server, script)
    assert result.status == "failed" and result.doc_url is None
    assert server.docs == {} and server.drafts == {}


async def test_one_publish_is_partial(settings, server, validated):
    server.fail_next["create_document"] = RuntimeError("docs down")
    script = [call("publish_doc"), call("upsert_gmail_draft"), final("Doc failed, draft created.")]
    result, _ = await agent(settings, server, script)
    assert result.status == "partial" and result.doc_url is None and result.draft_id == "draft1"


async def test_unknown_tool_and_bad_json_are_results_not_crashes(settings, server, validated):
    bad = call("get_pulse")
    bad["tool_calls"][0]["function"]["arguments"] = "{not json"
    script = [call("send_message"), bad, final("stopped")]
    result, model = await agent(settings, server, script)
    assert [t["ok"] for t in result.trace] == [False, False]
    contents = [json.loads(m["content"]) for m in model.chat_calls[-1] if m["role"] == "tool"]
    assert contents[0] == {"ok": False, "error": "unknown tool"} and "JSON" in contents[1]["error"]
    assert result.status == "failed" and result.reason is None


async def test_trace_written_to_run_json(settings, server, validated):
    script = [call("get_run_report"), call("publish_doc"), call("upsert_gmail_draft"), final("ok")]
    result, _ = await agent(settings, server, script)
    rj = run_json(settings)
    assert rj["agent_status"] == "ok" and rj["agent_reason"] is None
    assert rj["agent_trace"] == result.trace
    assert set(rj["agent_trace"][0]) == {"step", "tool", "args", "ok", "ms"}
    assert rj["agent_trace"][1]["args"] == {"week": WEEK}


async def test_groq_error_returns_failed_with_hint(settings, server, validated):
    result, _ = await agent(settings, server, [groq.GroqError("connection refused")])
    assert (result.status, result.reason) == ("failed", "llm_unavailable")
    assert "pulse run" in result.final_message and result.steps == 0
    assert run_json(settings)["agent_status"] == "failed"
