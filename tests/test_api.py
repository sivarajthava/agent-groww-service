import json
import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from groww_pulse.api import RunStore, Trigger, WeeklyScheduler, create_app, due_week, is_week
from groww_pulse.config import load_settings
from groww_pulse.runner import RunResult

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    monkeypatch.delenv("PULSE_PUBLISH", raising=False)
    return load_settings().model_copy(update={"data_dir": tmp_path})


def write_week(settings, week: str, **files) -> None:
    d = settings.data_dir / "runs" / week
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        name = name.replace("_md", ".md").replace("_json", ".json")
        (d / name).write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


class FakeRunner:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []
        self.release = threading.Event()
        self.release.set()

    def __call__(self, settings, week, dry_run):
        self.calls.append((week, dry_run))
        self.release.wait(5)
        return RunResult(week, "dry_run" if dry_run else "ok", None, None, None, [], None)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def client(settings, runner):
    return TestClient(create_app(settings, token=TOKEN, runner=runner, schedule=False))


def test_refuses_to_start_without_a_strong_token(settings):
    with pytest.raises(RuntimeError, match="PULSE_API_TOKEN"):
        create_app(settings, token="", schedule=False)
    with pytest.raises(RuntimeError):
        create_app(settings, token="short", schedule=False)


def test_health_is_public(client):
    assert client.get("/health").json() == {"ok": True}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-wrong-token"}, {"Authorization": TOKEN}])
@pytest.mark.parametrize("method,path", [("get", "/runs"), ("get", "/runs/2026-W43"),
                                         ("post", "/runs/2026-W43/trigger")])
def test_every_data_route_needs_the_token(client, headers, method, path):
    assert getattr(client, method)(path, headers=headers).status_code == 401


def test_lists_weeks_newest_first_and_skips_other_folders(client, settings):
    write_week(settings, "2026-W42", run_json={"status": "failed"})
    write_week(settings, "2026-W43", run_json={"status": "ok"}, themes_json={"top": ["t1"]})
    (settings.data_dir / "runs" / "not-a-week").mkdir()
    body = client.get("/runs", headers=AUTH).json()
    assert [r["week"] for r in body["runs"]] == ["2026-W43", "2026-W42"]
    assert body["runs"][0] == {"week": "2026-W43", "run": {"status": "ok"}, "themes": {"top": ["t1"]}}
    assert body["runs"][1]["themes"] is None


def test_detail_drops_quote_provenance(client, settings):
    quotes = {"quotes": [{"text": "Slow support", "rating": 1, "review_id": "abc123secret", "theme_id": "t1"}]}
    write_week(settings, "2026-W43", run_json={"status": "ok"}, quotes_json=quotes, pulse_md="# Pulse\n")
    body = client.get("/runs/2026-W43", headers=AUTH).json()
    assert body["quotes"] == [{"text": "Slow support", "rating": 1, "theme_id": "t1"}]
    assert body["pulse_md"] == "# Pulse\n"
    assert "abc123secret" not in json.dumps(body)


def test_detail_of_a_partial_week(client, settings):
    write_week(settings, "2026-W42", run_json={"status": "failed", "error": "no_reviews"})
    body = client.get("/runs/2026-W42", headers=AUTH).json()
    assert body["quotes"] == [] and body["pulse_md"] is None and body["themes"] is None


@pytest.mark.parametrize("week", ["2026-W01", "2026-W00", "2026-W54", "2025-W53", "..", "not-a-week"])
def test_unknown_or_invalid_weeks_are_404(client, settings, week):
    (settings.data_dir / "runs" / "not-a-week").mkdir(parents=True)
    assert client.get(f"/runs/{week}", headers=AUTH).status_code == 404


def test_trigger_starts_a_dry_run(client, runner):
    r = client.post("/runs/2026-W43/trigger", headers=AUTH)
    assert r.status_code == 202 and r.json() == {"week": "2026-W43", "dry_run": True, "accepted": True}
    for _ in range(100):
        if runner.calls:
            break
        threading.Event().wait(0.01)
    assert runner.calls == [("2026-W43", True)]


def test_trigger_refuses_publishing_unless_enabled(client, runner):
    assert client.post("/runs/2026-W43/trigger?dry_run=false", headers=AUTH).status_code == 403
    assert runner.calls == []


def test_trigger_publishes_when_enabled(client, runner, monkeypatch):
    monkeypatch.setenv("PULSE_PUBLISH", "true")
    assert client.post("/runs/2026-W43/trigger?dry_run=false", headers=AUTH).status_code == 202


def test_second_trigger_for_a_running_week_is_409(client, runner):
    runner.release.clear()
    try:
        assert client.post("/runs/2026-W43/trigger", headers=AUTH).status_code == 202
        assert client.post("/runs/2026-W43/trigger", headers=AUTH).status_code == 409
        assert client.get("/runs", headers=AUTH).json()["active"] == ["2026-W43"]
    finally:
        runner.release.set()


def test_is_week():
    assert is_week("2026-W43") and is_week("2026-W53")
    assert not any(is_week(w) for w in ["2026-W00", "2026-W54", "2025-W53", "2026-43", "../x"])


# 2026-10-19 is the Monday of 2026-W43. 09:00 IST = 03:30 UTC.
@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 19, 3, 29, tzinfo=UTC), None),          # Monday 08:59 IST: not yet
    (datetime(2026, 10, 19, 3, 30, tzinfo=UTC), "2026-W43"),    # Monday 09:00 IST
    (datetime(2026, 10, 22, 12, 0, tzinfo=UTC), "2026-W43"),    # missed slot (restart): catches up later in the week
    (datetime(2026, 10, 18, 20, 0, tzinfo=UTC), None),          # Monday 01:30 IST of W43, before 09:00
    (datetime(2026, 10, 25, 19, 0, tzinfo=UTC), None),          # Monday 00:30 IST of W44, before 09:00
])
def test_due_week(settings, now, expected):
    assert due_week(now, RunStore(settings.data_dir)) == expected


def test_due_week_skips_a_week_that_already_ran(settings):
    write_week(settings, "2026-W43", run_json={"status": "failed"})
    assert due_week(datetime(2026, 10, 20, 6, 0, tzinfo=UTC), RunStore(settings.data_dir)) is None


def test_scheduler_tick_runs_dry_unless_publish_enabled(settings, runner, monkeypatch):
    clock = lambda: datetime(2026, 10, 19, 4, 0, tzinfo=UTC)  # noqa: E731
    store = RunStore(settings.data_dir)
    assert WeeklyScheduler(store, Trigger(settings, runner), clock=clock).tick() == "2026-W43"
    monkeypatch.setenv("PULSE_PUBLISH", "true")
    trigger = Trigger(settings, runner)
    WeeklyScheduler(store, trigger, clock=clock).tick()
    for _ in range(100):
        if len(runner.calls) == 2:
            break
        threading.Event().wait(0.01)
    assert sorted(runner.calls) == [("2026-W43", False), ("2026-W43", True)]
