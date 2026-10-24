import pytest

from groww_pulse.models import Theme
from groww_pulse.themes.rank import rank_themes, score, with_wow_delta

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


def test_with_wow_delta_matches_by_name():
    now = [_t("x", 0.30, 0.5, name="Withdrawal Delays"), _t("y", 0.20, 0.5, name="New Theme")]
    prev = [_t("old", 0.25, 0.5, name="Withdrawal Delays")]
    out = with_wow_delta(now, prev)
    assert out[0].wow_delta == pytest.approx(0.05)
    assert out[1].wow_delta is None
    assert now[0].wow_delta is None  # inputs untouched (frozen dataclasses)
