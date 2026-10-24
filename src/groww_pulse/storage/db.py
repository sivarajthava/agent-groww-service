"""SQLite store for scrubbed reviews, weekly themes and the run audit. Never holds PII."""

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from groww_pulse.models import Review, Theme

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    rating INTEGER NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    date TEXT NOT NULL,
    app_version TEXT,
    lang TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_date ON reviews(date);
CREATE TABLE IF NOT EXISTS themes (
    run_week TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    review_ids TEXT NOT NULL,
    count INTEGER NOT NULL,
    share REAL NOT NULL,
    avg_rating REAL NOT NULL,
    neg_share REAL NOT NULL,
    wow_delta REAL,
    PRIMARY KEY (run_week, theme_id)
);
CREATE TABLE IF NOT EXISTS review_themes (
    run_week TEXT NOT NULL,
    review_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    similarity REAL NOT NULL,
    PRIMARY KEY (run_week, review_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_week TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    doc_url TEXT,
    draft_id TEXT
);
"""

FINAL_STATUSES = ("ok", "partial", "failed", "dry_run")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- reviews ---

    def upsert_reviews(self, reviews: list[Review]) -> int:
        """Insert new reviews and refresh edited ones; return the number of new rows."""
        count = "SELECT COUNT(*) FROM reviews"
        with self.conn:
            before = self.conn.execute(count).fetchone()[0]
            self.conn.executemany(
                """INSERT INTO reviews (id, source, rating, title, text, date, app_version, lang, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET rating=excluded.rating, title=excluded.title, text=excluded.text,
                       date=excluded.date, app_version=excluded.app_version, lang=excluded.lang""",
                [
                    (r.id, r.source, r.rating, r.title, r.text, r.date.isoformat(), r.app_version, r.lang, _now())
                    for r in reviews
                ],
            )
            return self.conn.execute(count).fetchone()[0] - before

    def reviews_in_window(self, since: date, until: date) -> list[Review]:
        rows = self.conn.execute(
            "SELECT * FROM reviews WHERE date BETWEEN ? AND ? ORDER BY date, id", (since.isoformat(), until.isoformat())
        )
        return [
            Review(
                id=r["id"],
                source=r["source"],
                rating=r["rating"],
                title=r["title"],
                text=r["text"],
                date=date.fromisoformat(r["date"]),
                app_version=r["app_version"],
                lang=r["lang"],
            )
            for r in rows
        ]

    # --- themes ---

    def save_themes(self, week: str, themes: list[Theme]) -> None:
        """Replace the week's themes, so a re-run never leaves stale ones behind."""
        with self.conn:
            self.conn.execute("DELETE FROM themes WHERE run_week = ?", (week,))
            self.conn.executemany(
                "INSERT INTO themes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (week, t.id, i, t.name, t.description, json.dumps(list(t.review_ids)), t.count, t.share,
                     t.avg_rating, t.neg_share, t.wow_delta)
                    for i, t in enumerate(themes)
                ],
            )

    def load_themes(self, week: str) -> list[Theme]:
        rows = self.conn.execute("SELECT * FROM themes WHERE run_week = ? ORDER BY position", (week,))
        return [
            Theme(
                id=r["theme_id"],
                name=r["name"],
                description=r["description"],
                review_ids=tuple(json.loads(r["review_ids"])),
                count=r["count"],
                share=r["share"],
                avg_rating=r["avg_rating"],
                neg_share=r["neg_share"],
                wow_delta=r["wow_delta"],
            )
            for r in rows
        ]

    def previous_theme_names(self, week: str) -> list[str]:
        """Theme names from the latest week before `week` (ISO week strings sort chronologically)."""
        row = self.conn.execute("SELECT MAX(run_week) FROM themes WHERE run_week < ?", (week,)).fetchone()
        return [t.name for t in self.load_themes(row[0])] if row[0] else []

    def save_assignments(self, week: str, rows: list[tuple[str, str, float]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM review_themes WHERE run_week = ?", (week,))
            self.conn.executemany(
                "INSERT INTO review_themes VALUES (?, ?, ?, ?)", [(week, rid, tid, sim) for rid, tid, sim in rows]
            )

    # --- runs ---

    def record_run(self, week: str, status: str, doc_url: str | None = None, draft_id: str | None = None) -> None:
        """Upsert the week's run row. A missing doc_url/draft_id keeps the value already recorded."""
        now = _now()
        finished = now if status in FINAL_STATUSES else None
        with self.conn:
            self.conn.execute(
                """INSERT INTO runs (run_week, started_at, finished_at, status, doc_url, draft_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_week) DO UPDATE SET status=excluded.status, finished_at=excluded.finished_at,
                       doc_url=COALESCE(excluded.doc_url, runs.doc_url),
                       draft_id=COALESCE(excluded.draft_id, runs.draft_id)""",
                (week, now, finished, status, doc_url, draft_id),
            )

    def get_run(self, week: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_week = ?", (week,)).fetchone()
        return dict(row) if row else None
