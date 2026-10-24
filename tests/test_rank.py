from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from groww_pulse.models import Theme
from conftest import make_review
from groww_pulse.themes.rank import rank_themes, score, weekly_delta

W = (0.6, 0.3, 0.1)


def _t(tid, share, neg, count=10, wow=None, name=None):
    return Theme(tid, name or tid.title(), "d", tuple(f"{tid}{i}" for i in range(count)), count, share, 3.0, neg, wow)


def test_score_formula():
    assert score(_t("a", 0.3, 0.9, wow=0.05), W) == pytest.approx(0.6 * 0.3 + 0.3 * 0.9 + 0.1 * 0.05)


def test_score_ignores_negative_or_missing_wow():
    assert score(_t("a", 0.3, 0.5, wow=-0.2), W) == score(_t("a", 0.3, 0.5, wow=None), W)


def test_rank_prefers_negative_heavy_theme():
    a, b = _t("a", 0.30, 0.9), _t("b", 0.35, 0.1)
    assert rank_themes([b, a], W)[0].id == "a"


def test_rank_returns_top_3():
    themes = [_t(c, s, 0.5) for c, s in zip("abcde", (0.1, 0.3, 0.2, 0.25, 0.15))]
    assert [t.id for t in rank_themes(themes, W)] == ["b", "d", "c"]


def test_rank_breaks_ties_by_count():
    themes = [_t("a", 0.2, 0.5, count=5), _t("b", 0.2, 0.5, count=9)]
    assert [t.id for t in rank_themes(themes, W, top_n=2)] == ["b", "a"]


def test_rank_with_fewer_themes_than_top_n():
    assert len(rank_themes([_t("a", 0.2, 0.5)], W)) == 1


CONFIG_WEIGHTS = tuple(yaml.safe_load(
    (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))["rank_weights"])


def test_rank_issue_themes_above_generic_praise():
    """F5: real-data shapes (share, neg_share). A merged praise theme must not take a top-3 slot."""
    themes = [_t("praise", 0.37, 0.19), _t("support", 0.11, 0.88), _t("failures", 0.16, 0.72),
              _t("requests", 0.12, 0.49), _t("charts", 0.09, 0.51), _t("charges", 0.14, 0.41)]
    top = [t.id for t in rank_themes(themes, CONFIG_WEIGHTS)]
    assert "praise" not in top and top[:2] == ["support", "failures"]
    a, b = _t("a", 0.30, 0.9), _t("b", 0.35, 0.1)  # the negative-heavy theme still wins with the new weights
    assert rank_themes([b, a], CONFIG_WEIGHTS)[0].id == "a"


def _week_reviews(prefix: str, monday: date, n: int) -> list:
    return [make_review(f"{prefix}{i}", "text", day=monday + timedelta(days=i % 7)) for i in range(n)]


def test_weekly_delta_latest_vs_previous_week():
    """F10: theme A has 10 of 40 reviews in 2026-W43 and 4 of 40 in W42 -> wow_delta 0.25 - 0.10 = 0.15."""
    cur = _week_reviews("c", date(2026, 10, 19), 40)
    prev = _week_reviews("p", date(2026, 10, 12), 40)
    older = _week_reviews("o", date(2026, 10, 5), 30)  # other weeks in the window don't count
    by_id = {r.id: r for r in cur + prev + older}
    theme_a = Theme("a", "A", "d", tuple(r.id for r in cur[:10] + prev[:4] + older[:20]), 34, 0.3, 3.0, 0.5, None)
    theme_b = Theme("b", "B", "d", tuple(r.id for r in cur[10:] + prev[4:]), 66, 0.6, 3.0, 0.5, None)
    out = weekly_delta([theme_a, theme_b], by_id, "2026-W43")
    assert out[0].wow_delta == pytest.approx(0.15)
    assert out[1].wow_delta == pytest.approx(30 / 40 - 36 / 40)
    assert [t.id for t in out] == ["a", "b"] and out[0].share == 0.3  # nothing else changes


def test_weekly_delta_none_below_min_reviews():
    cur = _week_reviews("c", date(2026, 10, 19), 10)
    prev = _week_reviews("p", date(2026, 10, 12), 40)
    by_id = {r.id: r for r in cur + prev}
    theme = Theme("a", "A", "d", tuple(by_id), 50, 1.0, 3.0, 0.5, 0.4)
    assert weekly_delta([theme], by_id, "2026-W43")[0].wow_delta is None
    assert weekly_delta([theme], by_id, "2026-W43", min_week_reviews=10)[0].wow_delta == pytest.approx(0.0)
