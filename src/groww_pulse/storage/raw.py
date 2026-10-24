"""Raw source snapshots: an audit trail and a fallback when the source is down.

Snapshots may contain PII (user names), so they live outside SQLite, are gitignored, and are purged after 14 days.
"""

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from groww_pulse.models import RawReview


def _dir(root: Path, source: str) -> Path:
    return Path(root) / "raw" / source


def save_snapshot(root: Path, source: str, run_date: date, items: list[RawReview]) -> Path:
    path = _dir(root, source) / f"{run_date.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{**asdict(r), "at": r.at.isoformat()} for r in items]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _snapshots(root: Path, source: str) -> list[tuple[date, Path]]:
    d = _dir(root, source)
    out = []
    for p in d.glob("*.json") if d.is_dir() else []:
        try:
            out.append((date.fromisoformat(p.stem), p))
        except ValueError:
            continue  # not a snapshot file
    return sorted(out)


def load_latest_snapshot(root: Path, source: str) -> list[RawReview] | None:
    snaps = _snapshots(root, source)
    if not snaps:
        return None
    rows = json.loads(snaps[-1][1].read_text(encoding="utf-8"))
    return [RawReview(**{**r, "at": datetime.fromisoformat(r["at"])}) for r in rows]


def purge_snapshots(root: Path, today: date, older_than_days: int = 14) -> int:
    """Delete snapshots of every source older than `older_than_days`; return how many were removed."""
    raw = Path(root) / "raw"
    removed = 0
    for source_dir in raw.iterdir() if raw.is_dir() else []:
        for day, path in _snapshots(root, source_dir.name):
            if (today - day).days > older_than_days:
                path.unlink()
                removed += 1
    return removed
