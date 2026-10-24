"""Common interface for review sources, so later stages never depend on a specific store."""

from datetime import date
from typing import Protocol, runtime_checkable

from groww_pulse.models import RawReview


@runtime_checkable
class ReviewSource(Protocol):
    name: str

    def fetch(self, since: date, until: date) -> list[RawReview]: ...
