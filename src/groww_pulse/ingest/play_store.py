"""Google Play Store review adapters: the public review listing and a Play Console CSV export."""

import csv
import io
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import google_play_scraper
from google_play_scraper import Sort

from groww_pulse.models import RawReview


class PlayStorePublicSource:
    """Pages through public reviews, newest first, until the window's start is passed.

    No login, no cookies; one request per `throttle_s` seconds.
    """

    name = "play_store"

    def __init__(
        self,
        app_id: str,
        lang: str,
        country: str,
        throttle_s: float = 1.0,
        page_size: int = 200,
        fetch_page: Callable = google_play_scraper.reviews,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.app_id = app_id
        self.lang = lang
        self.country = country
        self.throttle_s = throttle_s
        self.page_size = page_size
        self.fetch_page = fetch_page
        self.sleep = sleep

    def fetch(self, since: date, until: date) -> list[RawReview]:
        out: list[RawReview] = []
        seen: set[str] = set()
        token = None
        first = True
        while True:
            if not first:
                self.sleep(self.throttle_s)
            first = False
            items, token = self.fetch_page(
                self.app_id,
                lang=self.lang,
                country=self.country,
                sort=Sort.NEWEST,
                count=self.page_size,
                continuation_token=token,
            )
            for it in items:
                day = it["at"].date()
                if not since <= day <= until or not it.get("content") or it["reviewId"] in seen:
                    continue
                seen.add(it["reviewId"])
                out.append(
                    RawReview(
                        source_review_id=it["reviewId"],
                        rating=int(it["score"]),
                        title=None,  # Play reviews have no title
                        text=it["content"],
                        at=it["at"],
                        app_version=it.get("reviewCreatedVersion"),
                        user_name=it.get("userName"),
                    )
                )
            # The scraper always returns a token object; `token.token is None` marks the last page.
            if not items or min(it["at"].date() for it in items) < since:
                break
            if token is None or getattr(token, "token", None) is None:
                break
        return out


class PlayStoreCsvSource:
    """Reads a Play Console review export (UTF-16 or UTF-8 CSV)."""

    name = "play_store"

    def __init__(self, path: Path):
        self.path = Path(path)

    def _rows(self) -> list[dict]:
        raw = self.path.read_bytes()
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        return list(csv.DictReader(io.StringIO(raw.decode(encoding))))

    def fetch(self, since: date, until: date) -> list[RawReview]:
        out: list[RawReview] = []
        for row in self._rows():
            text = (row.get("Review Text") or "").strip()
            if not text:
                continue
            at = datetime.fromisoformat(row["Review Submit Date and Time"])
            if at.tzinfo is not None:
                at = at.astimezone(UTC).replace(tzinfo=None)
            if not since <= at.date() <= until:
                continue
            link = row.get("Review Link") or ""
            review_id = parse_qs(urlparse(link).query).get("reviewId", [link])[0]
            if not review_id:
                continue
            out.append(
                RawReview(
                    source_review_id=review_id,
                    rating=int(row["Star Rating"]),
                    title=(row.get("Review Title") or "").strip() or None,
                    text=text,
                    at=at,
                    app_version=(row.get("App Version Name") or "").strip() or None,
                )
            )
        return out
