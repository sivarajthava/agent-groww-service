"""HTTP API for hosted deployments (doc/deployment.md): read-only access to data/runs/<week>/ for the web frontend,
an authenticated trigger, and the weekly scheduler.

The API serves the same files the CLI writes and nothing else. It never returns review text beyond the validated
pulse and its 3 quotes, and quote provenance (review_id) is dropped before anything leaves the process.
Every route except /health needs `Authorization: Bearer $PULSE_API_TOKEN`; without a token the app refuses to start.
"""

import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from groww_pulse.config import Settings
from groww_pulse.runner import RunResult, run_pipeline
from groww_pulse.weeks import IST, current_iso_week, week_window

log = logging.getLogger("groww_pulse.api")

RUN_FILES = ("run.json", "themes.json", "quotes.json", "pulse.md")
SCHEDULE_AT_IST = time(9, 0)  # Mondays 09:00 IST, as in the cron / Task Scheduler setups

Runner = Callable[[Settings, str, bool], RunResult]


def _default_runner(settings: Settings, week: str, dry_run: bool) -> RunResult:
    return run_pipeline(settings, week, dry_run=dry_run)


def is_week(value: str) -> bool:
    """A real ISO week id (YYYY-Www); W53 only in years that have one."""
    try:
        week_window(value, 1)
    except ValueError:
        return False
    return True


def publish_enabled() -> bool:
    """Runs are dry-runs unless PULSE_PUBLISH=true (same switch as the GitHub Actions workflow)."""
    return os.environ.get("PULSE_PUBLISH", "").strip().lower() == "true"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # missing, or being written by a run in progress


class RunStore:
    """Read access to data/runs/<week>/. Week ids are checked before any path is built."""

    def __init__(self, data_dir: Path):
        self.runs = data_dir / "runs"

    def weeks(self) -> list[str]:
        if not self.runs.is_dir():
            return []
        return sorted((p.name for p in self.runs.iterdir() if p.is_dir() and is_week(p.name)), reverse=True)

    def has_run(self, week: str) -> bool:
        return (self.runs / week / "run.json").exists()

    def summary(self, week: str) -> dict:
        d = self.runs / week
        return {"week": week, "run": _read_json(d / "run.json"), "themes": _read_json(d / "themes.json")}

    def detail(self, week: str) -> dict | None:
        d = self.runs / week
        if not is_week(week) or not d.is_dir():
            return None
        quotes = (_read_json(d / "quotes.json") or {}).get("quotes", [])
        try:
            pulse_md = (d / "pulse.md").read_text(encoding="utf-8")
        except OSError:
            pulse_md = None
        return {
            **self.summary(week),
            # Provenance stays on disk for the audit log; only text, rating and theme are served.
            "quotes": [{"text": q["text"], "rating": q["rating"], "theme_id": q["theme_id"]} for q in quotes],
            "pulse_md": pulse_md,
        }


class Trigger:
    """Runs the pipeline for a week in a background thread; one run per week at a time in this process.

    run_pipeline's file lock still guards against a run started elsewhere (e.g. `pulse run` in a shell).
    """

    def __init__(self, settings: Settings, runner: Runner = _default_runner):
        self.settings, self.runner = settings, runner
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def start(self, week: str, dry_run: bool) -> bool:
        with self._lock:
            if week in self._active:
                return False
            self._active.add(week)
        threading.Thread(target=self._run, args=(week, dry_run), name=f"pulse-{week}", daemon=True).start()
        return True

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._active)

    def _run(self, week: str, dry_run: bool) -> None:
        try:
            r = self.runner(self.settings, week, dry_run)
            log.info("run week=%s status=%s flags=%s error=%s", r.week, r.status, ",".join(r.flags) or "-", r.error)
        except Exception:
            log.exception("run week=%s crashed", week)
        finally:
            with self._lock:
                self._active.discard(week)


def due_week(now_utc: datetime, store: RunStore) -> str | None:
    """The current IST week if its Monday 09:00 IST has passed and it has no run yet, else None.

    Checking "has no run.json" instead of firing at an exact minute means a restart or a deploy during the
    slot only delays the weekly run; and a week that already ran (ok or failed) is never repeated automatically.
    """
    now_ist = now_utc.astimezone(UTC).replace(tzinfo=None) + IST
    week = current_iso_week(now_ist.date())
    monday = now_ist.date() - timedelta(days=now_ist.weekday())
    if now_ist < datetime.combine(monday, SCHEDULE_AT_IST) or store.has_run(week):
        return None
    return week


class WeeklyScheduler:
    def __init__(self, store: RunStore, trigger: Trigger, interval_s: float = 600,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.store, self.trigger, self.interval_s, self.clock = store, trigger, interval_s, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> str | None:
        week = due_week(self.clock(), self.store)
        if week and self.trigger.start(week, dry_run=not publish_enabled()):
            log.info("scheduled run started for %s (publish=%s)", week, publish_enabled())
            return week
        return None

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("scheduler tick failed")
                self._stop.wait(self.interval_s)

        self._thread = threading.Thread(target=loop, name="pulse-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def create_app(settings: Settings, *, token: str | None = None, runner: Runner = _default_runner,
               schedule: bool | None = None) -> FastAPI:
    token = token if token is not None else os.environ.get("PULSE_API_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("PULSE_API_TOKEN must be set to a random secret of at least 16 characters")
    if schedule is None:
        schedule = os.environ.get("PULSE_SCHEDULE", "true").strip().lower() != "false"

    store = RunStore(settings.data_dir)
    trigger = Trigger(settings, runner)
    scheduler = WeeklyScheduler(store, trigger)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if schedule:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Groww Weekly Pulse API", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    def auth(authorization: str = Header("")) -> None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value.encode(), token.encode()):
            raise HTTPException(401, "missing or invalid bearer token")

    def week_param(week: str) -> str:
        if not is_week(week):
            raise HTTPException(404, "no such week")
        return week

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/runs", dependencies=[Depends(auth)])
    def list_runs() -> dict:
        return {"runs": [store.summary(w) for w in store.weeks()], "active": trigger.active()}

    @app.get("/runs/{week}", dependencies=[Depends(auth)])
    def get_run(week: str = Depends(week_param)) -> dict:
        detail = store.detail(week)
        if detail is None:
            raise HTTPException(404, "no such week")
        return detail

    @app.post("/runs/{week}/trigger", status_code=202, dependencies=[Depends(auth)])
    def trigger_run(week: str = Depends(week_param), dry_run: bool = Query(True)) -> dict:
        if not dry_run and not publish_enabled():
            raise HTTPException(403, "publishing is disabled on this deployment (set PULSE_PUBLISH=true)")
        if not trigger.start(week, dry_run):
            raise HTTPException(409, f"a run for {week} is already in progress")
        return {"week": week, "dry_run": dry_run, "accepted": True}

    app.state.store, app.state.trigger, app.state.scheduler = store, trigger, scheduler
    return app
