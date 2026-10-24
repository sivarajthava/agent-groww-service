from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from groww_pulse import cli
from groww_pulse.runner import RunResult
from groww_pulse.weeks import current_iso_week

runner = CliRunner()


class Calls(list):
    status: dict


@pytest.fixture
def calls(monkeypatch):
    """Replace settings loading and run_pipeline; returns the list of (week, kwargs) run_pipeline received."""
    received = Calls()
    status = {"value": "ok"}

    def fake_run_pipeline(settings, week, **kw):
        received.append((week, kw))
        return RunResult(week, status["value"], Path("data/runs") / week / "pulse.md",
                         "https://docs.example.com/d/doc1" if status["value"] != "dry_run" else None,
                         "draft1" if status["value"] == "ok" else None, ["low_volume"],
                         "pulse: validation failed" if status["value"] == "failed" else None)

    monkeypatch.setattr(cli, "load_settings", lambda path: object())
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    received.status = status
    return received


def test_defaults_to_current_week(calls):
    result = runner.invoke(cli.app, ["run"])
    assert result.exit_code == 0, result.output
    week, kw = calls[0]
    assert week == current_iso_week(date.today())
    assert kw == {"dry_run": False, "from_stage": "ingest"}
    assert "status=ok" in result.output and "doc_url=https://docs.example.com/d/doc1" in result.output
    assert "draft_id=draft1" in result.output and "flags=low_volume" in result.output


def test_run_options_are_passed_through(calls):
    result = runner.invoke(cli.app, ["run", "--week", "2026-W43", "--dry-run", "--stage", "themes"])
    assert result.exit_code == 0, result.output
    assert calls == [("2026-W43", {"dry_run": True, "from_stage": "themes"})]


def test_publish_is_run_from_publish_stage(calls):
    result = runner.invoke(cli.app, ["publish", "--week", "2026-W43"])
    assert result.exit_code == 0, result.output
    assert calls == [("2026-W43", {"dry_run": False, "from_stage": "publish"})]


def test_publish_requires_week(calls):
    assert runner.invoke(cli.app, ["publish"]).exit_code != 0
    assert calls == []


@pytest.mark.parametrize("bad", ["2026-43", "2026-W4", "W43", "2026-W54"])
def test_rejects_bad_week_format(calls, bad):
    result = runner.invoke(cli.app, ["run", "--week", bad])
    assert result.exit_code != 0
    assert "YYYY-Www" in result.output
    assert calls == []


def test_rejects_unknown_stage(calls):
    result = runner.invoke(cli.app, ["run", "--stage", "deploy"])
    assert result.exit_code != 0 and calls == []


@pytest.mark.parametrize("status, code", [("ok", 0), ("dry_run", 0), ("partial", 2), ("failed", 1)])
def test_exit_codes_by_status(calls, status, code):
    calls.status["value"] = status
    result = runner.invoke(cli.app, ["run", "--week", "2026-W43"])
    assert result.exit_code == code, result.output
    assert f"status={status}" in result.output
    if status == "failed":
        assert "pulse: validation failed" in result.output


@pytest.mark.parametrize("args", [["--help"], ["run", "--help"], ["publish", "--help"], ["agent", "--help"]])
def test_help_is_ascii(args):
    """Windows consoles default to cp1252; a non-ASCII arrow in help text crashed `pulse --help`."""
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0 and result.output.isascii()


@pytest.mark.parametrize("status, code", [("ok", 0), ("partial", 2), ("failed", 1)])
def test_agent_command_wires_and_exits_by_status(monkeypatch, status, code):
    from groww_pulse.agent.loop import AgentResult
    from groww_pulse.llm import GroqClient
    from groww_pulse.publish.mcp_client import McpGateway

    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    seen = {}

    async def fake_run_agent(ctx, llm, tools, goal=None):
        seen.update(ctx=ctx, llm=llm, tools=tools, goal=goal)
        return AgentResult(ctx.week, status, "Outcome: published.\nRan: pulse.\nProblems: none.",
                           "https://docs.example.com/d/doc1" if status != "failed" else None,
                           "draft1" if status == "ok" else None, 5, [],
                           "agent_step_limit" if status == "failed" else None)

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    result = runner.invoke(cli.app, ["agent", "--week", "2026-W43", "--goal", "Re-publish only"])
    assert result.exit_code == code, result.output
    assert "Outcome: published." in result.output and f"status={status}" in result.output
    assert seen["ctx"].week == "2026-W43" and seen["goal"] == "Re-publish only"
    assert isinstance(seen["llm"], GroqClient) and seen["ctx"].deps.llm is seen["llm"]  # stages use the same client
    gw = seen["tools"].gateway_factory("docs")
    assert isinstance(gw, McpGateway) and gw.name == "docs" and gw.server == seen["ctx"].settings.mcp.docs
    assert seen["tools"].gateway_factory("gmail").server == seen["ctx"].settings.mcp.gmail
    if status == "failed":
        assert "agent_step_limit" in result.output


def test_agent_command_defaults_to_current_week_and_rejects_bad_week(monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    seen = {}

    async def fake_run_agent(ctx, llm, tools, goal=None):
        from groww_pulse.agent.loop import AgentResult

        seen["week"], seen["goal"] = ctx.week, goal
        return AgentResult(ctx.week, "ok", "done", None, None, 1, [])

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    assert runner.invoke(cli.app, ["agent"]).exit_code == 0
    assert seen == {"week": current_iso_week(date.today()), "goal": None}
    bad = runner.invoke(cli.app, ["agent", "--week", "2026-43"])
    assert bad.exit_code != 0 and "YYYY-Www" in bad.output
