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
from groww_pulse.weeks import ist_date


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
        page_attempts: int = 3,
    ):
        self.app_id = app_id
        self.lang = lang
        self.country = country
        self.throttle_s = throttle_s
        self.page_size = page_size
        self.fetch_page = fetch_page
        self.sleep = sleep
        self.page_attempts = page_attempts
        self.partial = False  # set when a later page failed for good: the result holds the earlier pages only

    def fetch(self, since: date, until: date) -> list[RawReview]:
        out: list[RawReview] = []
        seen: set[str] = set()
        token = None
        first = True
        self.partial = False
        while True:
            if not first:
                self.sleep(self.throttle_s)
            for attempt in range(self.page_attempts):
                try:
                    items, token = self.fetch_page(
                        self.app_id,
                        lang=self.lang,
                        country=self.country,
                        sort=Sort.NEWEST,
                        count=self.page_size,
                        continuation_token=token,
                    )
                    break
                except Exception:
                    if attempt == self.page_attempts - 1:
                        if first:
                            raise  # nothing fetched: the runner falls back to the latest snapshot
                        self.partial = True
                        return out
                    self.sleep(2**attempt)
            first = False
            for it in items:
                # The scraper gives naive local time (datetime.fromtimestamp); store naive UTC.
                it = {**it, "at": it["at"].astimezone(UTC).replace(tzinfo=None)}
                day = ist_date(it["at"])
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
            if not items or min(ist_date(it["at"].astimezone(UTC).replace(tzinfo=None)) for it in items) < since:
                break
            if token is None or getattr(token, "token", None) is None:
                break
        return out


REQUIRED_CSV_COLUMNS = ("Review Submit Date and Time", "Star Rating", "Review Text", "Review Link")


class PlayStoreCsvSource:
    """Reads a Play Console review export (UTF-16 or UTF-8 CSV)."""

    name = "play_store"

    def __init__(self, path: Path):
        self.path = Path(path)

    def _rows(self) -> list[dict]:
        raw = self.path.read_bytes()
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        reader = csv.DictReader(io.StringIO(raw.decode(encoding)))
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{self.path.name} is missing required column(s): {', '.join(missing)}")
        return list(reader)

    def fetch(self, since: date, until: date) -> list[RawReview]:
        out: list[RawReview] = []
        for row in self._rows():
            text = (row.get("Review Text") or "").strip()
            if not text:
                continue
            at = datetime.fromisoformat(row["Review Submit Date and Time"])
            if at.tzinfo is not None:
                at = at.astimezone(UTC).replace(tzinfo=None)
            if not since <= ist_date(at) <= until:
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
