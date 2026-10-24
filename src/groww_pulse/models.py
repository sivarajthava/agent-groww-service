"""Typed domain objects passed between pipeline stages."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


@dataclass(frozen=True)
class RawReview:
    """A review as fetched from the source. May contain PII; never stored in SQLite."""

    source_review_id: str
    rating: int
    title: str | None
    text: str
    at: datetime
    app_version: str | None
    user_name: str | None = None


@dataclass(frozen=True)
class Review:
    """A normalized, PII-scrubbed review. `id` is a salted hash of the source review id."""

    id: str
    source: Literal["play_store"]
    rating: int
    title: str | None
    text: str
    date: date
    app_version: str | None
    lang: str


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    description: str
    review_ids: tuple[str, ...]
    count: int
    share: float
    avg_rating: float
    neg_share: float
    wow_delta: float | None


@dataclass(frozen=True)
class Quote:
    text: str
    rating: int
    review_id: str
    theme_id: str


@dataclass(frozen=True)
class PulseDraft:
    theme_summaries: tuple[str, str, str]
    actions: tuple[str, str, str]


@dataclass(frozen=True)
class Pulse:
    week: str
    window: tuple[date, date]
    total_reviews: int
    avg_rating: float
    themes: tuple[Theme, ...]
    quotes: tuple[Quote, ...]
    draft: PulseDraft
    markdown: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    failures: tuple[str, ...]
