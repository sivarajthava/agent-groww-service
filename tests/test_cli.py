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


@pytest.mark.parametrize("args", [["--help"], ["run", "--help"], ["publish", "--help"]])
def test_help_is_ascii(args):
    """Windows consoles default to cp1252; a non-ASCII arrow in help text crashed `pulse --help`."""
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0 and result.output.isascii()
