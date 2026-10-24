"""Map raw reviews onto the scrubbed Review schema: window filter, dedup, PII scrub, language tag."""

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import date

from langdetect import DetectorFactory, LangDetectException, detect

from groww_pulse.models import RawReview, Review
from groww_pulse.weeks import ist_date
from groww_pulse.privacy.scrubber import HINGLISH_WORDS, redact_user_name, scrub_text

DetectorFactory.seed = 0  # langdetect is randomised; a fixed seed makes tags reproducible

SOURCE = "play_store"
HINGLISH_SHARE = 0.2  # Latin-script text with at least this share of Hinglish words is tagged "hi-Latn"
SHORT_WORDS = 12  # below this, langdetect is unreliable: a non-English verdict on Latin text becomes "en"
MIN_WORDS = 8  # reviews with fewer words are dropped: too short to cluster or quote

# Emoji glue that isn't category "So": variation selectors, zero-width joiner, keycap, skin-tone modifiers.
_EMOJI_PARTS = {0xFE0E, 0xFE0F, 0x200D, 0x20E3, *range(0x1F3FB, 0x1F400)}


def remove_emoji(text: str) -> str:
    """Drop emoji and other pictographic symbols (Unicode "So"); letters, digits, ₹ and punctuation stay."""
    return "".join(c for c in text if unicodedata.category(c) != "So" and ord(c) not in _EMOJI_PARTS)


def _clean(text: str) -> str:
    return " ".join(remove_emoji(unicodedata.normalize("NFC", text)).split())


def word_count(text: str) -> int:
    """Whitespace-separated tokens containing a letter or digit (works for Devanagari too)."""
    return sum(any(c.isalnum() for c in tok) for tok in text.split())


def _text_key(text: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed: catches copy-pasted reviews."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def is_noise(text: str, min_words: int = MIN_WORDS) -> bool:
    """Fewer than `min_words` words, or no letters at all (emoji, digits, punctuation)."""
    return word_count(text) < min_words or not any(c.isalpha() for c in text)


def detect_lang(text: str) -> str:
    """"hi" for Devanagari, "hi-Latn" for Hinglish, else langdetect, corrected for short Latin-script reviews."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return "und"
    if sum("ऀ" <= c <= "ॿ" for c in chars) / len(chars) > 0.3:
        return "hi"
    words = re.findall(r"[^\W\d_]+", text.lower())
    latin = bool(words) and all(w.isascii() for w in words)
    if latin and sum(w in HINGLISH_WORDS for w in words) / len(words) >= HINGLISH_SHARE:
        return "hi-Latn"
    try:
        lang = detect(text)
    except LangDetectException:
        return "und"
    # On this app's English listing, short Latin-script reviews are English; langdetect guesses no/af/so.
    if lang != "en" and latin and len(words) < SHORT_WORDS:
        return "en"
    return lang


def review_id(salt: str, source_review_id: str) -> str:
    """The stored id: a salted hash, so it can't be traced back to the Play review."""
    return hashlib.sha256(f"{salt}{SOURCE}{source_review_id}".encode()).hexdigest()


def normalize(
    raw: Iterable[RawReview], since: date, until: date, salt: str, min_words: int = MIN_WORDS
) -> list[Review]:
    """Scrubbed, emoji-free, deduplicated reviews inside [since, until] with at least `min_words` words.

    User names are never copied, and a reviewer's own name inside their review is redacted.
    """
    out: list[Review] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for r in raw:
        day = ist_date(r.at)
        if not since <= day <= until:
            continue
        rid = review_id(salt, r.source_review_id)
        if rid in seen_ids:
            continue
        text = scrub_text(redact_user_name(_clean(r.text), r.user_name))
        key = _text_key(text)
        if is_noise(text, min_words) or key in seen_texts:
            continue
        seen_texts.add(key)
        seen_ids.add(rid)
        title = scrub_text(redact_user_name(_clean(r.title), r.user_name)) if r.title else None
        out.append(
            Review(
                id=rid,
                source=SOURCE,
                rating=r.rating,
                title=title or None,
                text=text,
                date=day,
                app_version=r.app_version,
                lang=detect_lang(text),
            )
        )
    return out
