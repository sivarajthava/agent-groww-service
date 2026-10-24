"""ISO week <-> date window helpers. The ISO week (YYYY-Www) is the run's idempotency key."""

import re
from datetime import date, timedelta

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def current_iso_week(today: date) -> str:
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


def week_window(week: str, weeks_back: int) -> tuple[date, date]:
    """Return (since, until): until is the Sunday of `week`; the window spans `weeks_back` whole weeks."""
    m = _WEEK_RE.match(week)
    if not m:
        raise ValueError(f"invalid ISO week {week!r}, expected YYYY-Www")
    until = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 7)
    since = until - timedelta(days=7 * weeks_back - 1)
    return since, until
