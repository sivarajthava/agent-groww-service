"""Download public Play Store reviews into a raw snapshot: python -m groww_pulse.ingest [--weeks N] [--week YYYY-Www].

Writes data/raw/play_store/<today>.json (gitignored, may contain user names, purged after 14 days).
Prints only counts and dates, never review text or user names.
"""

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import yaml

from groww_pulse.ingest.play_store import PlayStorePublicSource
from groww_pulse.storage.raw import save_snapshot
from groww_pulse.weeks import current_iso_week, week_window


def main(argv: list[str] | None = None) -> int:
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) if Path("config.yaml").exists() else {}
    p = argparse.ArgumentParser(prog="python -m groww_pulse.ingest")
    p.add_argument("--week", default=current_iso_week(date.today()), help="ISO week whose Sunday ends the window")
    p.add_argument("--weeks", type=int, default=cfg.get("max_window_weeks", 12), help="window length in weeks")
    p.add_argument("--data-dir", type=Path, default=Path(cfg.get("data_dir", "data")))
    args = p.parse_args(argv)

    since, until = week_window(args.week, args.weeks)
    app_id = cfg.get("app_id", "com.nextbillion.groww")
    print(f"Fetching {app_id} reviews {since} .. {until} ({args.weeks} weeks), newest first, 1 req/s ...")
    source = PlayStorePublicSource(app_id, cfg.get("lang", "en"), cfg.get("country", "in"))
    items = source.fetch(since, until)
    path = save_snapshot(args.data_dir, source.name, date.today(), items)

    print(f"Saved {len(items)} reviews -> {path}")
    if not items:
        print("WARNING: no reviews fetched", file=sys.stderr)
        return 1
    days = sorted(r.at.date() for r in items)
    print(f"Date range: {days[0]} .. {days[-1]}")
    weeks = Counter(current_iso_week(d) for d in days)
    print("Per ISO week: " + ", ".join(f"{w}={n}" for w, n in sorted(weeks.items())))
    ratings = Counter(r.rating for r in items)
    print("Ratings: " + ", ".join(f"{s}*={ratings[s]}" for s in range(1, 6)))
    # The scraper ends pagination silently on network errors, so check the window was actually covered.
    if days[0] > since + timedelta(days=1):
        print(f"WARNING: oldest review is {days[0]}, window starts {since}; the download may be incomplete",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
