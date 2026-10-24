"""Map raw reviews onto the scrubbed Review schema: window filter, dedup, PII scrub, language tag."""

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import date

from langdetect import DetectorFactory, LangDetectException, detect

from groww_pulse.models import RawReview, Review
from groww_pulse.privacy.scrubber import HINGLISH_WORDS, scrub_text

DetectorFactory.seed = 0  # langdetect is randomised; a fixed seed makes tags reproducible

SOURCE = "play_store"
HINGLISH_SHARE = 0.2  # Latin-script text with at least this share of Hinglish words is tagged "hi-Latn"
SHORT_WORDS = 12  # below this, langdetect is unreliable: a non-English verdict on Latin text becomes "en"


def _clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _text_key(text: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed: catches copy-pasted reviews."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def is_noise(text: str) -> bool:
    """Fewer than 3 word tokens, or no letters at all (emoji, digits, punctuation)."""
    return len(re.findall(r"\w+", text)) < 3 or not any(c.isalpha() for c in text)


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


def normalize(raw: Iterable[RawReview], since: date, until: date, salt: str) -> list[Review]:
    """Scrubbed, deduplicated reviews inside [since, until]. User names are never copied.

    Text dedup applies only to non-noise reviews: identical short reviews ("Good app") are real,
    separate ratings and are kept for counts.
    """
    out: list[Review] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for r in raw:
        day = r.at.date()
        if not since <= day <= until:
            continue
        rid = hashlib.sha256(f"{salt}{SOURCE}{r.source_review_id}".encode()).hexdigest()
        if rid in seen_ids:
            continue
        text = scrub_text(_clean(r.text))
        if not is_noise(text):
            key = _text_key(text)
            if key in seen_texts:
                continue
            seen_texts.add(key)
        seen_ids.add(rid)
        title = scrub_text(_clean(r.title)) if r.title else None
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
