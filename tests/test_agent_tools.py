import json
from datetime import date

import pytest
from fakes.fake_mcp import fake_session_factory, make_fake_server
from test_runner import TODAY, WEEK, ListSource, llm, no_sleep, run_json
from test_stages import _raw

from groww_pulse.agent.tools import AgentTools
from groww_pulse.config import load_settings
from groww_pulse.publish.mcp_client import McpGateway
from groww_pulse.runner import RunContext, RunDeps, run_pipeline
from groww_pulse.storage.db import Database
from groww_pulse.themes.embed import HashEmbedder


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    return load_settings().model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def server():
    return make_fake_server()


def make_tools(settings, server, week=WEEK) -> AgentTools:
    deps = RunDeps(source=ListSource(_raw()), llm=llm(), embedder=HashEmbedder(), today=TODAY, sleep=lambda s: None)
    factory = fake_session_factory(server)
    return AgentTools(RunContext(settings, week, deps),
                      lambda name: McpGateway(getattr(settings.mcp, name), name, session_factory=factory,
                                              sleep=no_sleep))


@pytest.fixture
def validated(settings):
    """A fixture run directory: a dry run leaves a validated pulse.md and run.json for WEEK."""
    result = run_pipeline(settings, WEEK, dry_run=True, source=ListSource(_raw()), llm=llm(),
                          embedder=HashEmbedder(), today=TODAY, sleep=lambda s: None)
    assert result.status == "dry_run", result.error
    return result.pulse_path


def set_validation(settings, ok: bool) -> None:
    path = settings.data_dir / "runs" / WEEK / "run.json"
    rj = json.loads(path.read_text(encoding="utf-8"))
    rj["validation"] = {"ok": ok, "failures": [] if ok else ["word_count: 300 > 250"]}
    path.write_text(json.dumps(rj), encoding="utf-8")


def test_schemas_take_only_week_and_never_offer_publish_as_a_stage(settings, server):
    schemas = {s["function"]["name"]: s["function"] for s in make_tools(settings, server).schemas()}
    assert set(schemas) == {"run_stage", "get_run_report", "get_pulse", "publish_doc", "upsert_gmail_draft"}
    assert all(s["type"] == "function" for s in make_tools(settings, server).schemas())
    for name, fn in schemas.items():
        props = fn["parameters"]["properties"]
        assert set(props) == ({"week", "stage"} if name == "run_stage" else {"week"})
        assert props["week"]["pattern"] == r"^\d{4}-W\d{2}$"
        assert fn["description"]
    assert schemas["run_stage"]["parameters"]["properties"]["stage"]["enum"] == ["ingest", "normalize", "themes", "pulse"]


async def test_publish_refused_before_validation(settings, server, validated):
    """Review Focus #7: a write tool called before validation passed is refused; nothing reaches MCP."""
    set_validation(settings, False)
    tools = make_tools(settings, server)
    assert await tools.dispatch("publish_doc", {"week": WEEK}) == {"ok": False, "error": "pulse not validated"}
    assert await tools.dispatch("upsert_gmail_draft", {"week": WEEK}) == {"ok": False, "error": "pulse not validated"}
    assert server.docs == {} and server.drafts == {}


async def test_publish_refused_without_any_run(settings, server):
    tools = make_tools(settings, server)
    assert (await tools.dispatch("publish_doc", {"week": WEEK}))["ok"] is False
    assert server.docs == {}


async def test_publish_uses_pulse_md_from_disk(settings, server, validated):
    validated.write_bytes("# Groww — Weekly Review Pulse\n\nedited on disk ★5\n".encode("utf-8"))
    tools = make_tools(settings, server)
    doc = await tools.dispatch("publish_doc", {"week": WEEK})
    assert doc == {"ok": True, "doc_url": "https://docs.example.com/d/doc1"}
    assert server.docs["doc1"]["content"].encode("utf-8") == validated.read_bytes()

    draft = await tools.dispatch("upsert_gmail_draft", {"week": WEEK})
    assert draft == {"ok": True, "draft_id": "draft1"}
    assert server.drafts["draft1"]["body"] == f"Doc: {doc['doc_url']}\n\n" + validated.read_text(encoding="utf-8")
    rj = run_json(settings)
    assert (rj["doc_url"], rj["draft_id"]) == (doc["doc_url"], "draft1")
    assert {c["server"] for c in rj["mcp_calls"]} == {"docs", "gmail"}
    with Database(settings.data_dir / "reviews.sqlite") as db:
        assert db.get_run(WEEK)["doc_url"] == doc["doc_url"] and db.get_run(WEEK)["draft_id"] == "draft1"
    assert server.sent == []


async def test_publish_error_comes_back_as_result(settings, server, validated):
    server.fail_next["create_document"] = RuntimeError("docs down")
    result = await make_tools(settings, server).dispatch("publish_doc", {"week": WEEK})
    assert result["ok"] is False and "McpCallError" in result["error"]


async def test_unknown_tool_returns_error_not_exception(settings, server):
    tools = make_tools(settings, server)
    assert await tools.dispatch("create_document", {"week": WEEK}) == {"ok": False, "error": "unknown tool"}
    assert await tools.dispatch("send_message", {}) == {"ok": False, "error": "unknown tool"}


@pytest.mark.parametrize("name, args", [
    ("run_stage", {"week": WEEK, "stage": "publish"}),
    ("run_stage", {"week": WEEK}),
    ("get_pulse", {"week": "2026-43"}),
    ("get_run_report", {"week": WEEK, "extra": 1}),
    ("publish_doc", {}),
    ("publish_doc", None),
])
async def test_bad_args_rejected_by_schema(settings, server, validated, name, args):
    result = await make_tools(settings, server).dispatch(name, args)
    assert result["ok"] is False and result["error"].startswith("invalid arguments")
    assert server.docs == {}


async def test_week_mismatch_rejected(settings, server, validated):
    tools = make_tools(settings, server)
    for name in ("publish_doc", "get_pulse", "get_run_report"):
        result = await tools.dispatch(name, {"week": "2026-W42"})
        assert result == {"ok": False, "error": f"week must be {WEEK}"}
    assert server.docs == {}


async def test_run_report_and_pulse(settings, server, validated):
    tools = make_tools(settings, server)
    report = await tools.dispatch("get_run_report", {"week": WEEK})
    assert report["ok"] is True and report["status"] == "dry_run"
    assert report["validation"] == {"ok": True, "failures": []}
    assert report["outputs"] == {"themes": True, "quotes": True, "pulse": True}
    assert report["counts"]["reviews"] == 41 and report["doc_url"] is None
    pulse = await tools.dispatch("get_pulse", {"week": WEEK})
    assert pulse == {"ok": True, "markdown": validated.read_text(encoding="utf-8")}


async def test_report_and_pulse_before_any_run(settings, server):
    tools = make_tools(settings, server)
    report = await tools.dispatch("get_run_report", {"week": WEEK})
    assert report["ok"] is True and report["status"] is None
    assert report["outputs"] == {"themes": False, "quotes": False, "pulse": False}
    assert (await tools.dispatch("get_pulse", {"week": WEEK}))["ok"] is False


async def test_tool_results_contain_no_review_text(settings, server):
    tools = make_tools(settings, server)
    results = [await tools.dispatch("get_run_report", {"week": WEEK})]
    for stage in ("ingest", "normalize", "themes", "pulse"):
        results.append(await tools.dispatch("run_stage", {"week": WEEK, "stage": stage}))
    assert all(r["ok"] for r in results[1:]), results
    results += [await tools.dispatch("get_run_report", {"week": WEEK}),
                await tools.dispatch("publish_doc", {"week": WEEK}),
                await tools.dispatch("upsert_gmail_draft", {"week": WEEK})]
    dumped = json.dumps(results)
    with Database(settings.data_dir / "reviews.sqlite") as db:
        texts = [r.text for r in db.reviews_in_window(date(2026, 8, 1), date(2026, 10, 25))]
    assert texts and not any(t[:30] in dumped for t in texts)
    assert results[1] == {"ok": True, "stage": "ingest", "counts": {"fetched": 41}, "flags": [], "error": None}
