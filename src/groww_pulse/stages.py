"""Stages 2–4 of the pipeline over stored data: snapshot → SQLite → themes/quotes → validated pulse.md.

python -m groww_pulse.stages {normalize,themes,pulse,all} [--week YYYY-Www] [--config config.yaml]

Each stage reads what the previous one stored and writes its own output, so any stage can be re-run:
- normalize: latest raw snapshot → normalized, scrubbed reviews upserted into data/reviews.sqlite
- themes:    SQLite reviews in the window → themes + assignments in SQLite, data/runs/<week>/themes.json, quotes.json
- pulse:     themes + quotes → LLM draft → validators → data/runs/<week>/pulse.md and run.json

Stage results and printed output hold counts and flags only, never review text.
"""

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np

from groww_pulse.config import Settings
from groww_pulse.llm import LLMClient
from groww_pulse.models import Quote, Review
from groww_pulse.normalize.normalize import normalize, review_id
from groww_pulse.prompts import prompt_sha256
from groww_pulse.pulse.generate import build_context
from groww_pulse.pulse.render import word_count
from groww_pulse.pulse.validate import ValidationFailed, generate_valid_pulse
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import load_latest_snapshot
from groww_pulse.themes.extract import extract_themes
from groww_pulse.themes.quotes import NotEnoughQuotes, select_quotes
from groww_pulse.themes.rank import rank_themes, weekly_delta
from groww_pulse.weeks import current_iso_week, week_window

SOURCE = "play_store"
PROMPTS = ("label_themes", "merge_themes", "write_pulse")


class StageError(Exception):
    """A stage can't run: missing input from an earlier stage, or too little data."""


def db_path(settings: Settings) -> Path:
    return settings.data_dir / "reviews.sqlite"


def run_dir(settings: Settings, week: str) -> Path:
    d = settings.data_dir / "runs" / week
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise StageError(f"{path} is missing; run the earlier stage first")
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_stage(settings: Settings, week: str, db: Database) -> dict:
    """Normalize the latest raw snapshot over the widest window and upsert it into SQLite."""
    raw = load_latest_snapshot(settings.data_dir, SOURCE)
    if raw is None:
        raise StageError(f"no raw snapshot in {settings.data_dir / 'raw' / SOURCE}; run python -m groww_pulse.ingest")
    since, until = week_window(week, settings.max_window_weeks)
    reviews = normalize(raw, since, until, settings.id_salt, min_words=settings.min_words)
    new = db.upsert_reviews(reviews)
    # Rows an earlier run stored for snapshot reviews the current rules drop (e.g. too short once a name is
    # redacted) would otherwise keep their old text; rows from other snapshots are left alone.
    kept = {r.id for r in reviews}
    removed = db.delete_reviews([rid for r in raw if since <= r.at.date() <= until
                                 and (rid := review_id(settings.id_salt, r.source_review_id)) not in kept])
    langs = Counter(r.lang for r in reviews)
    return {"raw": len(raw), "kept": len(reviews), "new": new, "removed": removed,
            **{f"lang_{k}": v for k, v in sorted(langs.items())}}


def _usable(reviews: list[Review]) -> int:
    return sum(r.lang == "en" for r in reviews)


def choose_window(settings: Settings, week: str, db: Database) -> tuple[tuple[date, date], list[Review], list[str]]:
    """window_weeks, widened to max_window_weeks under min_reviews usable reviews (flag `low_volume` if still)."""
    flags: list[str] = []
    window = week_window(week, settings.window_weeks)
    reviews = db.reviews_in_window(*window)
    if _usable(reviews) < settings.min_reviews and settings.max_window_weeks > settings.window_weeks:
        window = week_window(week, settings.max_window_weeks)
        reviews = db.reviews_in_window(*window)
        flags.append("window_widened")
    if _usable(reviews) < settings.min_reviews:
        flags.append("low_volume")
    return window, reviews, flags


def themes_stage(settings: Settings, week: str, db: Database, embedder, llm: LLMClient) -> dict:
    """Extract ≤ max_themes themes, rank them, pick 3 verbatim quotes; store themes, assignments and quotes."""
    window, reviews, flags = choose_window(settings, week, db)
    if not reviews:
        raise StageError(f"no reviews in SQLite for {window[0]}..{window[1]}; run the normalize stage first")
    diagnostics: dict = {}
    themes, centroids, emb = extract_themes(
        reviews, embedder, llm, db.previous_theme_names(week), max_themes=settings.max_themes,
        other_threshold=settings.other_threshold, k_min=settings.cluster_k_min, k_max=settings.cluster_k_max,
        diagnostics=diagnostics,
    )
    if not themes:
        raise StageError("no themes found (no English reviews in the window)")
    themes = weekly_delta(themes, {r.id: r for r in reviews}, week)
    db.save_themes(week, themes)
    db.save_assignments(week, [
        (rid, t.id, round(float(np.dot(emb[rid], centroids[t.id])), 4)) for t in themes for rid in t.review_ids
    ])
    top = rank_themes(themes, settings.rank_weights)
    if len(top) < 3:
        flags.append("few_themes")
    quotes = select_quotes(top, {r.id: r for r in reviews}, emb, centroids)  # NotEnoughQuotes propagates

    out = run_dir(settings, week)
    _write_json(out / "themes.json", {
        "week": week,
        "window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "total_reviews": len(reviews),
        "avg_rating": round(sum(r.rating for r in reviews) / len(reviews), 2),
        "flags": flags,
        "k": diagnostics.get("k", 0),
        "other_count": len(emb) - sum(t.count for t in themes),  # clustered reviews no theme was close to
        "top": [t.id for t in top],
        # review_ids stay in SQLite (review_themes); the JSON holds stats only
        "themes": [{k: v for k, v in asdict(t).items() if k != "review_ids"} for t in themes],
    })
    _write_json(out / "quotes.json", {"quotes": [asdict(q) for q in quotes]})
    db.record_run(week, "themes")
    return {"reviews": len(reviews), "themes": len(themes), "top": len(top), "quotes": len(quotes),
            "flags": flags}


def pulse_stage(settings: Settings, week: str, db: Database, llm: LLMClient, max_words: int = 250) -> dict:
    """Write the pulse with the LLM and validate it. pulse.md is written only when every check passes."""
    out = run_dir(settings, week)
    meta = _read_json(out / "themes.json")
    quotes = [Quote(**q) for q in _read_json(out / "quotes.json")["quotes"]]
    by_id = {t.id: t for t in db.load_themes(week)}
    top = [by_id[tid] for tid in meta["top"]]
    window = (date.fromisoformat(meta["window"]["start"]), date.fromisoformat(meta["window"]["end"]))
    reviews_by_id = {r.id: r for r in db.reviews_in_window(*window)}
    render_args = {"week": week, "window": window, "total_reviews": meta["total_reviews"],
                   "avg_rating": meta["avg_rating"], "top": top, "quotes": quotes}
    ctx = build_context(week, window, meta["total_reviews"], meta["avg_rating"], top, quotes, reviews_by_id,
                        word_budget=max_words)

    run = {"week": week, "window": meta["window"], "flags": meta["flags"],
           "counts": {"reviews": meta["total_reviews"], "themes": len(meta["themes"]), "quotes": len(quotes)},
           "model": None, "reasoning_effort": settings.reasoning_effort,
           "prompts": {p: prompt_sha256(p) for p in PROMPTS}}
    pulse_md = out / "pulse.md"
    try:
        # A reduced pulse (fewer than 3 themes) is allowed only when the themes stage flagged it.
        n_themes = min(len(top), 3) if "few_themes" in meta["flags"] else 3
        pulse = generate_valid_pulse(ctx, llm, render_args, reviews_by_id, n_themes=n_themes)
    except ValidationFailed as e:
        pulse_md.unlink(missing_ok=True)  # never leave an older, now-unvalidated pulse behind
        run.update(model=llm.last_model, validation={"ok": False, "failures": list(e.result.failures)},
                   status="failed")
        _write_json(out / "run.json", run)
        db.record_run(week, "failed")
        raise
    pulse_md.write_text(pulse.markdown, encoding="utf-8")
    run.update(model=llm.last_model, validation={"ok": True, "failures": []}, status="validated")
    _write_json(out / "run.json", run)
    db.record_run(week, "validated")
    return {"words": word_count(pulse.markdown), "path": str(pulse_md)}


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from groww_pulse.config import load_settings
    from groww_pulse.llm import GroqClient
    from groww_pulse.themes.embed import Embedder

    p = argparse.ArgumentParser(prog="python -m groww_pulse.stages")
    p.add_argument("stage", choices=("normalize", "themes", "pulse", "all"))
    p.add_argument("--week", default=current_iso_week(date.today()), help="ISO week whose Sunday ends the window")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = p.parse_args(argv)

    load_dotenv()  # GROQ_API_KEY for the groq SDK; Settings reads PULSE_ID_SALT from .env itself
    settings = load_settings(args.config)
    stages = ("normalize", "themes", "pulse") if args.stage == "all" else (args.stage,)
    if set(stages) & {"themes", "pulse"} and not os.environ.get("GROQ_API_KEY"):
        print("GROQ_API_KEY is not set (add it to .env); the themes and pulse stages need Groq", file=sys.stderr)
        return 2
    llm = GroqClient(settings.model, settings.fallback_model, settings.reasoning_effort, settings.temperature)

    with Database(db_path(settings)) as db:
        for stage in stages:
            try:
                if stage == "normalize":
                    result = normalize_stage(settings, args.week, db)
                elif stage == "themes":
                    result = themes_stage(settings, args.week, db, Embedder(), llm)
                else:
                    result = pulse_stage(settings, args.week, db, llm)
            except (StageError, NotEnoughQuotes) as e:
                print(f"{stage}: {e}", file=sys.stderr)
                return 1
            except ValidationFailed as e:
                print(f"{stage}: validation failed: {e}", file=sys.stderr)
                return 1
            print(f"{stage} {args.week}: " + ", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
