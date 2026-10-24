import numpy as np
import pytest

from conftest import make_review
from groww_pulse.llm import FakeLLM
from groww_pulse.models import Theme
from groww_pulse.themes.embed import HashEmbedder
from groww_pulse.themes.extract import extract_themes
from groww_pulse.themes.quotes import NotEnoughQuotes, select_quotes, trim_to_span
from test_extract import distinct_labeler

# --- trim_to_span ----------------------------------------------------------------------------


def test_trim_to_span_short_text_unchanged():
    assert trim_to_span("Withdrawal stuck. Support is slow.") == "Withdrawal stuck. Support is slow."


def test_trim_to_span_keeps_whole_sentences():
    text = "My money is stuck. " + "I raised a ticket and nobody replied. " * 3 + "Very bad experience overall today."
    out = trim_to_span(text, max_words=20)
    assert out == "My money is stuck. I raised a ticket and nobody replied. I raised a ticket and nobody replied.…"
    assert len(out.split()) <= 20


def test_trim_to_span_cuts_long_first_sentence_at_word_boundary():
    text = " ".join(f"w{i}" for i in range(50)) + ". Second sentence."
    out = trim_to_span(text, max_words=40)
    assert out == " ".join(f"w{i}" for i in range(40)) + "…"


def test_trim_to_span_is_a_prefix_of_the_text():
    text = "Charts lag a lot! Orders fail at open?! " + "Fix it now please team. " * 10
    assert text.startswith(trim_to_span(text, 12).rstrip("…"))


# --- select_quotes ---------------------------------------------------------------------------


@pytest.fixture
def extracted(topic_reviews):
    themes, centroids, emb = extract_themes(topic_reviews, HashEmbedder(dim=256), FakeLLM([distinct_labeler]), [], k_min=3, k_max=8)
    return themes, centroids, emb, {r.id: r for r in topic_reviews}


def test_one_quote_per_top_theme_in_order(extracted):
    themes, centroids, emb, by_id = extracted
    quotes = select_quotes(themes[:3], by_id, emb, centroids)
    assert [q.theme_id for q in quotes] == [t.id for t in themes[:3]]
    assert len({q.review_id for q in quotes}) == 3


def test_quote_is_substring_of_review(extracted):
    themes, centroids, emb, by_id = extracted
    for q in select_quotes(themes, by_id, emb, centroids):
        assert q.text.rstrip("…") in by_id[q.review_id].text
        assert q.rating == by_id[q.review_id].rating
        assert 8 <= len(q.text.split()) <= 40


def test_quote_sentiment_matches_theme(extracted):
    themes, centroids, emb, by_id = extracted
    for q, t in zip(select_quotes(themes, by_id, emb, centroids), themes):
        if t.neg_share >= 0.5:
            assert q.rating <= 2
        else:
            assert q.rating >= 4


def test_quote_is_most_typical_preferred_review(extracted):
    themes, centroids, emb, by_id = extracted
    t = themes[0]
    q = select_quotes([t], by_id, emb, centroids)[0]
    want_neg = t.neg_share >= 0.5
    pool = [rid for rid in t.review_ids if (by_id[rid].rating <= 2) == want_neg]
    best = max(pool, key=lambda rid: float(emb[rid] @ centroids[t.id]))
    assert q.review_id == best


def _theme(tid, ids, neg=0.9):
    return Theme(tid, tid.title(), "d", tuple(ids), len(ids), 0.3, 1.5, neg, None)


def _setup(reviews, themes):
    rng = np.random.default_rng(0)
    emb = {r.id: (v := rng.normal(size=8).astype(np.float32)) / np.linalg.norm(v) for r in reviews}
    cents = {}
    for t in themes:
        m = np.mean([emb[i] for i in t.review_ids], axis=0)
        cents[t.id] = m / np.linalg.norm(m)
    return {r.id: r for r in reviews}, emb, cents


LONG_NEG = "The withdrawal has been pending for six days and nobody replies"


def test_non_english_never_quoted():
    reviews = [make_review("a1", "पैसा नहीं आया अभी तक मेरा withdrawal अटका है", 1, lang="hi"),
               make_review("a2", "withdrawal ka paisa abhi tak nahi aaya bhai kya karu", 1, lang="hi-Latn"),
               make_review("a3", LONG_NEG, 1)]
    t = _theme("a", ["a1", "a2", "a3"])
    by_id, emb, cents = _setup(reviews, [t])
    with pytest.raises(NotEnoughQuotes, match="1 of 3"):  # the Hindi/Hinglish reviews never fill a slot
        select_quotes([t], by_id, emb, cents)
    reviews += [make_review("a4", LONG_NEG + " again", 1), make_review("a5", LONG_NEG + " still", 1)]
    t = _theme("a", ["a1", "a2", "a3", "a4", "a5"])
    by_id, emb, cents = _setup(reviews, [t])
    assert {q.review_id for q in select_quotes([t], by_id, emb, cents)} == {"a3", "a4", "a5"}


def test_fallback_when_theme_has_no_eligible_quote():
    reviews = [make_review("a1", LONG_NEG, 1), make_review("a2", LONG_NEG + " again", 1),
               make_review("b1", "too short", 1), make_review("b2", "bad app", 1),
               make_review("c1", "Order execution is slow at market open every single day", 1)]
    themes = [_theme("a", ["a1", "a2"]), _theme("b", ["b1", "b2"]), _theme("c", ["c1"])]
    by_id, emb, cents = _setup(reviews, themes)
    quotes = select_quotes(themes, by_id, emb, cents)
    assert len(quotes) == 3
    assert len({q.review_id for q in quotes}) == 3  # none duplicated
    assert quotes[1].theme_id == "a"  # theme b's slot is filled from theme a, and says so


def test_raises_when_three_quotes_impossible():
    reviews = [make_review("a1", LONG_NEG, 1), make_review("b1", "too short", 1), make_review("c1", "meh", 1)]
    themes = [_theme("a", ["a1"]), _theme("b", ["b1"]), _theme("c", ["c1"])]
    by_id, emb, cents = _setup(reviews, themes)
    with pytest.raises(NotEnoughQuotes, match="2 of 3|1 of 3"):
        select_quotes(themes, by_id, emb, cents)


def test_falls_back_to_other_sentiment_when_none_preferred():
    fine = LONG_NEG.replace("nobody replies", "it is fine now")
    reviews = [make_review(f"a{i}", fine + " ok" * i, 5) for i in range(3)]
    t = _theme("a", [r.id for r in reviews], neg=0.9)
    by_id, emb, cents = _setup(reviews, [t])
    quotes = select_quotes([t], by_id, emb, cents)
    assert [q.rating for q in quotes] == [5, 5, 5]


def test_reduced_pulse_takes_three_quotes_from_fewer_themes():
    """A single theme (tiny data set) still yields 3 distinct quotes, all tagged with that theme."""
    reviews = [make_review(f"a{i}", LONG_NEG + " again" * i, 1) for i in range(4)]
    t = _theme("a", [r.id for r in reviews])
    by_id, emb, cents = _setup(reviews, [t])
    quotes = select_quotes([t], by_id, emb, cents)
    assert len({q.review_id for q in quotes}) == 3 and {q.theme_id for q in quotes} == {"a"}
