import random
from datetime import date

import pytest

from groww_pulse.models import Review

# Four synthetic, PII-free topics with disjoint vocabularies, so a hashing embedder separates them.
TOPICS = {
    "withdrawal": ("withdrawal money stuck bank account credited pending days refund transfer settlement amount", 1),
    "crash": ("app crashes freezes login screen update loading blank restart error opening slow", 2),
    "charges": ("brokerage charges fees high hidden deducted expensive cost pricing plan annual commission", 4),
    "support": ("customer support care response ticket reply chat agent email escalation helpline callback", 1),
}


def make_review(rid: str, text: str, rating: int = 3, lang: str = "en", day: date = date(2026, 10, 20)) -> Review:
    return Review(rid, "play_store", rating, None, text, day, "15.2.0", lang)


def topic_text(topic: str, rng: random.Random, n_words: int = 10) -> str:
    words = TOPICS[topic][0].split()
    return " ".join(rng.sample(words, n_words)).capitalize() + "."


@pytest.fixture
def topic_reviews() -> list[Review]:
    """40 English reviews (10 per topic) + 4 Hindi/Hinglish + 1 off-topic review."""
    rng = random.Random(7)
    out = []
    for topic, (_, rating) in TOPICS.items():
        for i in range(10):
            r = rating if i < 8 else 6 - rating  # mostly the topic's sentiment, a couple the other way
            out.append(make_review(f"{topic[:2]}{i:02d}", topic_text(topic, rng), rating=r))
    out += [
        make_review("hi00", "पैसा नहीं आया अभी तक मेरा withdrawal अटका है", 1, lang="hi"),
        make_review("hi01", "पैसा नहीं आया अभी तक मेरा withdrawal अटका है भाई", 1, lang="hi"),
        make_review("hl00", "withdrawal ka paisa abhi tak nahi aaya bhai kya karu", 1, lang="hi-Latn"),
        make_review("hl01", "app bahut slow hai login nahi ho raha kya karu", 2, lang="hi-Latn"),
    ]
    out.append(make_review("zz00", "Zebra quantum violin orchestra galaxy pineapple telescope marathon", 3))
    return out
