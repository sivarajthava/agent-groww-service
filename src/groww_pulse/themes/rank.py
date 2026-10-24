"""Theme ranking: share, pain (negative share) and growth week over week."""

from dataclasses import replace

from groww_pulse.models import Theme


def score(t: Theme, weights: tuple[float, float, float]) -> float:
    w_share, w_neg, w_wow = weights
    return w_share * t.share + w_neg * t.neg_share + w_wow * max(t.wow_delta or 0, 0)


def rank_themes(themes: list[Theme], weights: tuple[float, float, float], top_n: int = 3) -> list[Theme]:
    return sorted(themes, key=lambda t: (score(t, weights), t.count), reverse=True)[:top_n]


def with_wow_delta(themes: list[Theme], previous: list[Theme]) -> list[Theme]:
    """Set wow_delta = share - last week's share for themes whose name matches; None otherwise."""
    prev = {t.name: t.share for t in previous}
    return [replace(t, wow_delta=t.share - prev[t.name] if t.name in prev else None) for t in themes]
