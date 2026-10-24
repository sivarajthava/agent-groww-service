from datetime import date

import pytest

from groww_pulse.weeks import current_iso_week, week_window


def test_week_window_ten_weeks():
    assert week_window("2026-W43", 10) == (date(2026, 8, 17), date(2026, 10, 25))


def test_week_window_until_is_sunday():
    since, until = week_window("2026-W01", 8)
    assert until == date(2026, 1, 4)
    assert until.isoweekday() == 7
    assert (until - since).days == 7 * 8 - 1


def test_current_iso_week():
    assert current_iso_week(date(2026, 10, 24)) == "2026-W43"


def test_current_iso_week_pads_single_digit():
    assert current_iso_week(date(2026, 1, 5)) == "2026-W02"


def test_week_window_rejects_bad_week_string():
    with pytest.raises(ValueError):
        week_window("2026-43", 10)
