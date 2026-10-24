import hashlib
import json
import random
import re
from datetime import date, datetime

import pytest

from conftest import TOPICS, topic_text
from groww_pulse.config import load_settings
from groww_pulse.llm import FakeLLM
from groww_pulse.models import RawReview, Review
from groww_pulse.pulse.validate import ValidationFailed
from groww_pulse.stages import StageError, db_path, normalize_stage, pulse_stage, themes_stage
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import save_snapshot
from groww_pulse.themes.embed import HashEmbedder

WEEK = "2026-W43"  # window ends Sunday 2026-10-25
NAMES = {"withdrawal": "Withdrawal Delays", "crash": "App Crashes", "charges": "High Charges",
         "support": "Slow Support"}
USER = "Priya Raman"
PHONE = "98765 43210"


def _raw(n_per_topic: int = 10, day: int = 20) -> list[RawReview]:
    rng = random.Random(3)
    out = []
    for topic, (_, rating) in TOPICS.items():
        for i in range(n_per_topic):
            out.append(RawReview(f"gp:{topic}:{i}", rating, None, topic_text(topic, rng),
                                 datetime(2026, 10, day - i % 5, 9), "15.2.0", USER))
    out.append(RawReview("gp:pii", 1, None, f"withdrawal stuck for days please call me on {PHONE} today",
                         datetime(2026, 10, 18, 9), "15.2.0", USER))
    return out


def _topic_of(text: str) -> str:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return max(TOPICS, key=lambda t: len(words & set(TOPICS[t][0].split())))


def labeler(system, user, schema):
    """Names each cluster after the topic most of its sample reviews come from (merge calls do the same)."""
    blocks = re.findall(r'<cluster id="(\d+)">(.*?)</cluster>', user, re.S)
    key = "clusters" if "clusters" in schema["properties"] else "mapping"
    return {key: [{"cluster": int(c), "name": NAMES[_topic_of(body)], "description": f"{NAMES[_topic_of(body)]}"}
                  for c, body in blocks]}


def writer(system, user, schema):
    """A valid draft for whatever themes the context lists."""
    ctx = json.loads(user.split("<data>")[1].split("</data>")[0])
    names = [t["name"] for t in ctx["themes"]]
    return {"theme_summaries": [f"Users report repeated problems with {n.lower()} this month." for n in names],
            "actions": [f"{n}: investigate the top complaints and track weekly volume." for n in names]}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    return load_settings().model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def db(settings):
    with Database(db_path(settings)) as d:
        yield d


def _themes_llm() -> FakeLLM:
    return FakeLLM([labeler] * 4)


def _all_output(settings) -> str:
    files = [p for p in (settings.data_dir / "runs").rglob("*") if p.is_file()]
    return "".join(p.read_text(encoding="utf-8") for p in files)


# --- normalize -------------------------------------------------------------------------------


def test_normalize_stage_stores_scrubbed_reviews_idempotently(settings, db):
    save_snapshot(settings.data_dir, "play_store", datetime(2026, 10, 24).date(), _raw())
    first = normalize_stage(settings, WEEK, db)
    assert first["raw"] == 41 and first["kept"] == 41 and first["new"] == 41
    assert normalize_stage(settings, WEEK, db)["new"] == 0
    dump = db.conn.execute("SELECT * FROM reviews").fetchall()
    text = " ".join(str(v) for row in dump for v in tuple(row))
    assert USER not in text and PHONE not in text and "[phone]" in text
    assert all(len(row["id"]) == 64 for row in dump)


def test_normalize_stage_removes_rows_the_current_rules_reject(settings, db):
    """A row an earlier run stored for a review that the current rules drop (too short, say) is deleted."""
    short = RawReview("gp:short", 5, None, "Priya Raman loves it", datetime(2026, 10, 18, 9), "15.2.0", USER)
    old = RawReview("gp:old", 4, None, "kept by a run for an earlier week, outside this window",
                    datetime(2026, 7, 1, 9), "15.2.0", USER)
    save_snapshot(settings.data_dir, "play_store", datetime(2026, 10, 24).date(), _raw() + [short, old])
    old_id = hashlib.sha256(f"{settings.id_salt}play_storegp:old".encode()).hexdigest()
    db.upsert_reviews([Review(old_id, "play_store", 4, None, "kept earlier", date(2026, 7, 1), "15.2.0", "en")])
    stale_id = hashlib.sha256(f"{settings.id_salt}play_storegp:short".encode()).hexdigest()
    db.upsert_reviews([Review(stale_id, "play_store", 5, None, "stale unredacted copy", date(2026, 10, 18),
                              "15.2.0", "en")])
    db.upsert_reviews([Review("other-snapshot-row", "play_store", 4, None, "kept from an older snapshot",
                              date(2026, 9, 1), "15.2.0", "en")])
    result = normalize_stage(settings, WEEK, db)
    assert result["removed"] == 1 and result["kept"] == 41
    ids = {r[0] for r in db.conn.execute("SELECT id FROM reviews")}
    assert stale_id not in ids and "other-snapshot-row" in ids and old_id in ids
    assert normalize_stage(settings, WEEK, db)["removed"] == 0

def test_normalize_stage_needs_a_snapshot(settings, db):
    with pytest.raises(StageError, match="ingest"):
        normalize_stage(settings, WEEK, db)


# --- themes ----------------------------------------------------------------------------------


def _through_themes(settings, db, week=WEEK, llm=None):
    save_snapshot(settings.data_dir, "play_store", datetime(2026, 10, 24).date(), _raw())
    normalize_stage(settings, week, db)
    return themes_stage(settings, week, db, HashEmbedder(), llm or _themes_llm())


def test_themes_stage_stores_themes_assignments_and_quotes(settings, db):
    result = _through_themes(settings, db)
    assert result["quotes"] == 3 and result["top"] == 3 and 3 <= result["themes"] <= 5
    themes = db.load_themes(WEEK)
    assert {t.name for t in themes} <= set(NAMES.values())
    assigned = db.conn.execute("SELECT COUNT(*) FROM review_themes WHERE run_week = ?", (WEEK,)).fetchone()[0]
    assert assigned == sum(t.count for t in themes)

    out = settings.data_dir / "runs" / WEEK
    meta = json.loads((out / "themes.json").read_text(encoding="utf-8"))
    assert meta["total_reviews"] == 41 and len(meta["top"]) == 3
    assert "review_ids" not in meta["themes"][0]
    quotes = json.loads((out / "quotes.json").read_text(encoding="utf-8"))["quotes"]
    stored = {r.id: r.text for r in db.reviews_in_window(*map(date.fromisoformat, meta["window"].values()))}
    assert all(q["text"].rstrip("…") in stored[q["review_id"]] for q in quotes)
    assert db.get_run(WEEK)["status"] == "themes"


def test_themes_stage_widens_window_and_flags_low_volume(settings, db):
    save_snapshot(settings.data_dir, "play_store", datetime(2026, 10, 24).date(), _raw(n_per_topic=3))
    normalize_stage(settings, WEEK, db)
    result = themes_stage(settings, WEEK, db, HashEmbedder(), _themes_llm())
    assert result["flags"][:2] == ["window_widened", "low_volume"]
    meta = json.loads((settings.data_dir / "runs" / WEEK / "themes.json").read_text(encoding="utf-8"))
    assert meta["window"]["start"] == "2026-08-03"  # 12 weeks back from 2026-10-25


def test_themes_stage_uses_previous_week(settings, db):
    _through_themes(settings, db, week="2026-W42")
    normalize_stage(settings, WEEK, db)
    llm = _themes_llm()
    themes_stage(settings, WEEK, db, HashEmbedder(), llm)
    assert "Withdrawal Delays" in llm.calls[0][1]  # last week's names offered for reuse
    assert any(t.wow_delta is not None for t in db.load_themes(WEEK))


def test_themes_stage_needs_reviews(settings, db):
    with pytest.raises(StageError, match="normalize"):
        themes_stage(settings, WEEK, db, HashEmbedder(), _themes_llm())


# --- pulse -----------------------------------------------------------------------------------


def test_pulse_stage_writes_validated_pulse_and_run_json(settings, db):
    _through_themes(settings, db)
    result = pulse_stage(settings, WEEK, db, FakeLLM([writer]))
    out = settings.data_dir / "runs" / WEEK
    md = (out / "pulse.md").read_text(encoding="utf-8")
    assert md.startswith("# Groww — Weekly Review Pulse") and result["words"] <= 250
    quotes = json.loads((out / "quotes.json").read_text(encoding="utf-8"))["quotes"]
    assert all(f'"{q["text"]}"' in md for q in quotes)
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["validation"] == {"ok": True, "failures": []} and run["status"] == "validated"
    assert run["model"] == "fake" and set(run["prompts"]) == {"label_themes", "merge_themes", "write_pulse"}
    assert db.get_run(WEEK)["status"] == "validated"


def test_pulse_stage_failure_leaves_no_pulse(settings, db):
    _through_themes(settings, db)
    out = settings.data_dir / "runs" / WEEK
    (out / "pulse.md").write_text("stale pulse from an earlier run", encoding="utf-8")

    def leaky(system, user, schema):
        d = writer(system, user, schema)
        d["theme_summaries"][0] = "Users wrote to help@groww.in about this."
        return d

    with pytest.raises(ValidationFailed, match="pii"):
        pulse_stage(settings, WEEK, db, FakeLLM([leaky]))
    assert not (out / "pulse.md").exists()
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["validation"]["ok"] is False and run["status"] == "failed"
    assert "help@groww.in" not in json.dumps(run)
    assert db.get_run(WEEK)["status"] == "failed"


def test_pulse_stage_needs_themes_stage(settings, db):
    with pytest.raises(StageError, match="themes.json"):
        pulse_stage(settings, WEEK, db, FakeLLM([writer]))


def test_stage_outputs_hold_no_user_names_or_raw_ids(settings, db):
    _through_themes(settings, db)
    pulse_stage(settings, WEEK, db, FakeLLM([writer]))
    text = _all_output(settings)
    assert USER not in text and PHONE not in text and "gp:" not in text
