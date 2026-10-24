"""Theme ranking: share, pain (negative share) and growth week over week."""

from dataclasses import replace
from datetime import timedelta

from groww_pulse.models import Review, Theme
from groww_pulse.weeks import week_monday


def score(t: Theme, weights: tuple[float, float, float]) -> float:
    w_share, w_neg, w_wow = weights
    return w_share * t.share + w_neg * t.neg_share + w_wow * max(t.wow_delta or 0, 0)


def rank_themes(themes: list[Theme], weights: tuple[float, float, float], top_n: int = 3) -> list[Theme]:
    return sorted(themes, key=lambda t: (score(t, weights), t.count), reverse=True)[:top_n]


def weekly_delta(
    themes: list[Theme], reviews_by_id: dict[str, Review], week: str, min_week_reviews: int = 20
) -> list[Theme]:
    """Set wow_delta = the theme's share of the reviews dated in ISO `week` minus its share in the week before.

    Both shares come from the current window, so the delta shows this week's movement; comparing two 10-week
    windows that share 9 weeks would always give ~0. None when either week has fewer than `min_week_reviews`.
    """
    start = week_monday(week)
    weeks = ((start, start + timedelta(days=6)), (start - timedelta(days=7), start - timedelta(days=1)))
    in_week = [{rid for rid, r in reviews_by_id.items() if lo <= r.date <= hi} for lo, hi in weeks]
    if min(len(ids) for ids in in_week) < min_week_reviews:
        return [replace(t, wow_delta=None) for t in themes]
    cur, prev = in_week
    return [replace(t, wow_delta=len(cur.intersection(t.review_ids)) / len(cur)
                    - len(prev.intersection(t.review_ids)) / len(prev)) for t in themes]
