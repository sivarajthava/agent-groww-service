import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from groww_pulse.ingest.base import ReviewSource
from groww_pulse.ingest.play_store import PlayStoreCsvSource, PlayStorePublicSource
from groww_pulse.models import RawReview

FIXTURES = Path(__file__).parent / "fixtures"
SINCE, UNTIL = date(2026, 8, 17), date(2026, 10, 25)


class FakeFetchPage:
    """Mimics google_play_scraper.reviews: serves fixture pages and returns a token object each time."""

    def __init__(self, pages=(1, 2, 3)):
        self.pages = [self._load(n) for n in pages]
        self.call_count = 0
        self.kwargs: list[dict] = []

    @staticmethod
    def _load(n):
        items = json.loads((FIXTURES / f"play_page_{n}.json").read_text(encoding="utf-8"))
        for it in items:
            it["at"] = datetime.fromisoformat(it["at"])
        return items

    def __call__(self, app_id, **kwargs):
        self.kwargs.append({"app_id": app_id, **kwargs})
        items = self.pages[self.call_count] if self.call_count < len(self.pages) else []
        self.call_count += 1
        more = self.call_count < len(self.pages)
        return items, SimpleNamespace(token=f"t{self.call_count}" if more else None)


def _public(fake, sleep=lambda s: None):
    return PlayStorePublicSource("com.nextbillion.groww", "en", "in", fetch_page=fake, sleep=sleep)


def test_public_source_paginates_until_since():
    fake = FakeFetchPage()
    out = _public(fake).fetch(SINCE, UNTIL)
    assert all(SINCE <= r.at.date() <= UNTIL for r in out)
    assert fake.call_count == 2  # page 2 reaches past `since`, page 3 is never requested


def test_public_source_maps_fields_and_skips_empty_and_duplicates():
    out = _public(FakeFetchPage()).fetch(SINCE, UNTIL)
    assert [r.source_review_id for r in out] == ["gp:rev-2", "gp:rev-3", "gp:rev-5", "gp:rev-6"]
    first = out[0]
    assert first == RawReview(
        source_review_id="gp:rev-2",
        rating=1,
        title=None,
        text="Withdrawal is stuck for five days now",
        at=datetime(2026, 10, 25, 23, 30),
        app_version="15.2.1",
        user_name="Test User 2",
    )
    assert out[1].app_version is None


def test_public_source_requests_newest_first_with_continuation():
    from google_play_scraper import Sort

    fake = FakeFetchPage()
    _public(fake).fetch(SINCE, UNTIL)
    assert fake.kwargs[0]["sort"] == Sort.NEWEST
    assert fake.kwargs[0]["lang"] == "en" and fake.kwargs[0]["country"] == "in"
    assert fake.kwargs[0]["continuation_token"] is None
    assert fake.kwargs[1]["continuation_token"].token == "t1"


def test_public_source_stops_when_token_exhausted():
    fake = FakeFetchPage(pages=(1,))  # one page, all newer than `since`, token.token is None
    out = _public(fake).fetch(SINCE, UNTIL)
    assert fake.call_count == 1
    assert len(out) == 2


def test_public_source_stops_on_empty_page():
    fake = FakeFetchPage(pages=())
    assert _public(fake).fetch(SINCE, UNTIL) == []
    assert fake.call_count == 1


def test_public_source_throttles_between_pages():
    sleeps = []
    _public(FakeFetchPage(), sleep=sleeps.append).fetch(SINCE, UNTIL)
    assert sleeps == [1.0]  # two pages -> one pause, none before the first request


def test_public_source_is_a_review_source():
    assert isinstance(_public(FakeFetchPage()), ReviewSource)
    assert PlayStoreCsvSource(FIXTURES / "play_export.csv").name == "play_store"


def test_csv_source_maps_columns():
    out = PlayStoreCsvSource(FIXTURES / "play_export.csv").fetch(SINCE, UNTIL)
    assert len(out) == 3
    r = out[0]
    assert r.source_review_id == "gp:AOqpTOE1"
    assert r.rating == 1
    assert r.title is None
    assert r.text == "Withdrawal stuck for 5 days, support not responding"
    assert r.at.date() == date(2026, 10, 20)
    assert r.app_version == "15.2.0"
    assert r.user_name is None
    assert out[1].title == "Love it"
    assert out[2].app_version is None


def test_csv_source_skips_rows_without_text():
    out = PlayStoreCsvSource(FIXTURES / "play_export.csv").fetch(SINCE, UNTIL)
    assert "gp:AOqpTOE4" not in {r.source_review_id for r in out}


def test_csv_source_filters_window():
    out = PlayStoreCsvSource(FIXTURES / "play_export.csv").fetch(date(2026, 9, 1), UNTIL)
    assert [r.source_review_id for r in out] == ["gp:AOqpTOE1", "gp:AOqpTOE2"]


def test_csv_source_reads_utf16_console_export(tmp_path):
    # Play Console exports are UTF-16 encoded.
    p = tmp_path / "export.csv"
    p.write_text((FIXTURES / "play_export.csv").read_text(encoding="utf-8"), encoding="utf-16")
    assert len(PlayStoreCsvSource(p).fetch(SINCE, UNTIL)) == 3
