"""Verbatim quote selection: quotes are cut from stored review text, never written by the LLM."""

import re

import numpy as np

from groww_pulse.guards import unsafe_for_quote
from groww_pulse.models import Quote, Review, Theme
from groww_pulse.privacy.scrubber import HINGLISH_WORDS

MIN_QUOTE_WORDS, MAX_QUOTE_WORDS = 8, 40
QUOTE_COUNT = 3  # the pulse always has exactly 3 quotes, even with fewer than 3 themes
# Hinglish words that are also English words or common typos ("aap" for app) in English reviews.
ENGLISH_COLLISIONS = frozenset({"aap", "tab", "ho", "pe", "par", "se", "mat", "bas", "din", "ham", "ji", "jo", "ab"})
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?=\s|$)")


class NotEnoughQuotes(Exception):
    pass


def trim_to_span(text: str, max_words: int = MAX_QUOTE_WORDS) -> str:
    """The longest run of whole sentences from the start with at most max_words words, plus "…" if cut.

    A first sentence longer than max_words is cut at a word boundary. The result (minus "…") is always
    an exact prefix of the text.
    """
    text = text.strip()
    if len(text.split()) <= max_words:
        return text
    best = None
    for m in _SENTENCE_END.finditer(text):
        if len(text[: m.end()].split()) > max_words:
            break
        best = m.end()
    if best is None:
        best = list(re.finditer(r"\S+", text))[max_words - 1].end()
    return text[:best].rstrip() + "…"


def is_code_mixed(text: str) -> bool:
    """True when English-tagged text contains Hinglish (langdetect lets short code-mixed reviews through)."""
    return any(w in HINGLISH_WORDS and w not in ENGLISH_COLLISIONS for w in re.findall(r"[a-z]+", text.lower()))


def _quote_text(r: Review) -> str | None:
    if r.lang != "en" or is_code_mixed(r.text) or unsafe_for_quote(r.text):
        return None
    text = trim_to_span(r.text)
    return text if MIN_QUOTE_WORDS <= len(text.split()) <= MAX_QUOTE_WORDS else None


def select_quotes(
    top: list[Theme],
    reviews_by_id: dict[str, Review],
    emb_by_id: dict[str, np.ndarray],
    centroids: dict[str, np.ndarray],
) -> list[Quote]:
    """3 quotes: one per theme in `top` order, the most typical eligible review matching the theme's sentiment.

    A theme with no eligible review, and any slot beyond the number of themes (a reduced pulse), gets the
    next-best unused review of any top theme; that quote's theme_id names the theme it really came from.
    """
    candidates: dict[str, list[tuple[float, str, str]]] = {}  # theme_id -> [(similarity, review_id, text)]
    for t in top:
        rows = []
        for rid in t.review_ids:
            r = reviews_by_id.get(rid)
            text = _quote_text(r) if r and rid in emb_by_id else None
            if text:
                rows.append((float(emb_by_id[rid] @ centroids[t.id]), rid, text))
        candidates[t.id] = sorted(rows, reverse=True)

    used: set[str] = set()
    slots: list[Quote | None] = []
    for t in top[:QUOTE_COUNT]:
        want_neg = t.neg_share >= 0.5
        rows = [row for row in candidates[t.id] if row[1] not in used]
        preferred = [row for row in rows if (reviews_by_id[row[1]].rating <= 2 if want_neg
                                             else reviews_by_id[row[1]].rating >= 4)]
        pick = (preferred or rows or [None])[0]
        if pick:
            used.add(pick[1])
            slots.append(Quote(pick[2], reviews_by_id[pick[1]].rating, pick[1], t.id))
        else:
            slots.append(None)
    slots += [None] * (QUOTE_COUNT - len(slots))

    for i, q in enumerate(slots):
        if q is None:
            spare = sorted(
                ((sim, rid, text, tid) for tid, rows in candidates.items() for sim, rid, text in rows if rid not in used),
                reverse=True,
            )
            if spare:
                _, rid, text, tid = spare[0]
                used.add(rid)
                slots[i] = Quote(text, reviews_by_id[rid].rating, rid, tid)

    quotes = [q for q in slots if q]
    if len(quotes) < QUOTE_COUNT:
        raise NotEnoughQuotes(
            f"found {len(quotes)} of {QUOTE_COUNT} quotes: top themes lack English reviews of "
            f"{MIN_QUOTE_WORDS}-{MAX_QUOTE_WORDS} words"
        )
    return quotes
