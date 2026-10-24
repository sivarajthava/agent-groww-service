from datetime import date, datetime, timedelta

import pytest

from groww_pulse.models import RawReview, Review, Theme
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import load_latest_snapshot, purge_snapshots, save_snapshot


def _raw(i: int) -> RawReview:
    return RawReview(f"gp:{i}", 4, None, f"review text {i}", datetime(2026, 10, 1, 12, 30), "15.2.0", f"User {i}")


def _review(i: int, day: date, text: str = "withdrawal is stuck") -> Review:
    return Review(f"h{i:02d}", "play_store", 2, None, text, day, None, "en")


def _theme(tid: str, name: str, review_ids=("h01",)) -> Theme:
    return Theme(tid, name, f"{name} description", tuple(review_ids), len(review_ids), 0.4, 2.1, 0.7, None)


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "reviews.sqlite") as d:
        yield d


# --- raw snapshots ---------------------------------------------------------------------------


def test_snapshot_roundtrip(tmp_path):
    items = [_raw(1), _raw(2)]
    path = save_snapshot(tmp_path, "play_store", date(2026, 10, 24), items)
    assert path == tmp_path / "raw" / "play_store" / "2026-10-24.json"
    assert load_latest_snapshot(tmp_path, "play_store") == items


def test_load_latest_snapshot_picks_newest_and_handles_missing(tmp_path):
    assert load_latest_snapshot(tmp_path, "play_store") is None
    save_snapshot(tmp_path, "play_store", date(2026, 10, 10), [_raw(1)])
    save_snapshot(tmp_path, "play_store", date(2026, 10, 24), [_raw(2)])
    assert load_latest_snapshot(tmp_path, "play_store") == [_raw(2)]


def test_purge_removes_only_old_snapshots(tmp_path):
    today = date(2026, 10, 24)
    old = save_snapshot(tmp_path, "play_store", today - timedelta(days=20), [_raw(1)])
    fresh = save_snapshot(tmp_path, "play_store", today - timedelta(days=3), [_raw(2)])
    assert purge_snapshots(tmp_path, today) == 1
    assert not old.exists() and fresh.exists()


def test_purge_without_raw_dir_is_noop(tmp_path):
    assert purge_snapshots(tmp_path, date(2026, 10, 24)) == 0


# --- database --------------------------------------------------------------------------------


def test_upsert_is_idempotent(db):
    r = _review(1, date(2026, 10, 1))
    assert db.upsert_reviews([r]) == 1
    assert db.upsert_reviews([r]) == 0
    assert db.reviews_in_window(date(2026, 1, 1), date(2026, 12, 31)) == [r]


def test_upsert_counts_only_new_rows_in_a_mixed_batch(db):
    db.upsert_reviews([_review(1, date(2026, 10, 1))])
    assert db.upsert_reviews([_review(1, date(2026, 10, 1)), _review(2, date(2026, 10, 2))]) == 1


def test_upsert_keeps_latest_text_of_an_edited_review(db):
    db.upsert_reviews([_review(1, date(2026, 10, 1), "old text")])
    db.upsert_reviews([_review(1, date(2026, 10, 1), "edited text")])
    assert db.reviews_in_window(date(2026, 1, 1), date(2026, 12, 31))[0].text == "edited text"


def test_reviews_in_window_bounds_inclusive(db):
    days = [date(2026, 8, 16), date(2026, 8, 17), date(2026, 9, 1), date(2026, 10, 25), date(2026, 10, 26)]
    db.upsert_reviews([_review(i, d) for i, d in enumerate(days)])
    got = db.reviews_in_window(date(2026, 8, 17), date(2026, 10, 25))
    assert [r.date for r in got] == [date(2026, 8, 17), date(2026, 9, 1), date(2026, 10, 25)]


def test_themes_roundtrip_and_resave_replaces(db):
    themes = [_theme("t1", "Withdrawal Delays", ("h01", "h02")), _theme("t2", "App Crashes")]
    db.save_themes("2026-W43", themes)
    assert db.load_themes("2026-W43") == themes
    db.save_themes("2026-W43", themes[:1])
    assert db.load_themes("2026-W43") == themes[:1]
    assert db.load_themes("2026-W40") == []


def test_previous_theme_names_returns_latest_prior_week(db):
    db.save_themes("2026-W40", [_theme("a", "Old Theme")])
    db.save_themes("2026-W42", [_theme("b", "Withdrawal Delays"), _theme("c", "KYC Issues")])
    db.save_themes("2026-W43", [_theme("d", "Current Week")])
    assert db.previous_theme_names("2026-W43") == ["Withdrawal Delays", "KYC Issues"]
    assert db.previous_theme_names("2026-W40") == []


def test_previous_theme_names_orders_across_year_boundary(db):
    db.save_themes("2025-W52", [_theme("a", "Last Year")])
    db.save_themes("2026-W01", [_theme("b", "New Year")])
    assert db.previous_theme_names("2026-W02") == ["New Year"]


def test_save_assignments_replaces_week_rows(db):
    db.save_assignments("2026-W43", [("h01", "t1", 0.9), ("h02", "t1", 0.8)])
    db.save_assignments("2026-W43", [("h01", "t2", 0.7)])
    rows = db.conn.execute("SELECT review_id, theme_id, similarity FROM review_themes").fetchall()
    assert [tuple(r) for r in rows] == [("h01", "t2", 0.7)]


def test_record_run_upserts_by_week(db):
    db.record_run("2026-W43", "running")
    db.record_run("2026-W43", "ok", doc_url="https://docs.example/d/1", draft_id="dr1")
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    run = db.get_run("2026-W43")
    assert run["status"] == "ok"
    assert run["doc_url"] == "https://docs.example/d/1" and run["draft_id"] == "dr1"
    assert run["started_at"] and run["finished_at"]


def test_record_run_keeps_known_doc_url_when_not_given(db):
    db.record_run("2026-W43", "partial", doc_url="https://docs.example/d/1")
    db.record_run("2026-W43", "ok", draft_id="dr1")
    run = db.get_run("2026-W43")
    assert run["doc_url"] == "https://docs.example/d/1" and run["draft_id"] == "dr1"


def test_get_run_missing_returns_none(db):
    assert db.get_run("2026-W01") is None


def test_data_persists_across_connections(tmp_path):
    path = tmp_path / "reviews.sqlite"
    with Database(path) as d:
        d.upsert_reviews([_review(1, date(2026, 10, 1))])
    with Database(path) as d:
        assert len(d.reviews_in_window(date(2026, 1, 1), date(2026, 12, 31))) == 1
