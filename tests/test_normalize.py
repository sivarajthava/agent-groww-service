import hashlib
from dataclasses import astuple
from datetime import date, datetime

import pytest

from groww_pulse.models import RawReview
from groww_pulse.normalize.normalize import detect_lang, is_noise, normalize

SINCE, UNTIL = date(2026, 8, 17), date(2026, 10, 25)
SALT = "test-salt"


def _raw(rid: str, text: str, at=datetime(2026, 10, 1, 9, 0), rating=2, user="Rahul Sharma", title=None):
    return RawReview(rid, rating, title, text, at, "15.2.0", user)


LONG = "my withdrawal has been stuck for five days now"  # 9 words


def test_id_is_salted_hash_of_source_id():
    (r,) = normalize([_raw("gp:1", LONG)], SINCE, UNTIL, SALT)
    assert r.id == hashlib.sha256(f"{SALT}play_storegp:1".encode()).hexdigest()
    assert r.source == "play_store"
    assert r.date == date(2026, 10, 1)
    assert r.rating == 2 and r.app_version == "15.2.0"


def test_drops_out_of_window():
    # `at` is naive UTC and windows are IST calendar days: IST midnight is 18:30 UTC the day before.
    raws = [
        _raw("gp:1", "too old to keep, this review is outside the window", at=datetime(2026, 8, 16, 18, 29)),
        _raw("gp:2", "first day of the window, this review must be kept", at=datetime(2026, 8, 16, 18, 30)),
        _raw("gp:3", "last day of the window, this review must be kept", at=datetime(2026, 10, 25, 18, 29)),
        _raw("gp:4", "too new to keep, this review is outside the window", at=datetime(2026, 10, 25, 18, 30)),
    ]
    assert [r.text[:9] for r in normalize(raws, SINCE, UNTIL, SALT)] == ["first day", "last day "]


def test_dedups_by_id_and_text():
    raws = [
        _raw("gp:1", "Withdrawal is stuck for 5 days, please fix it!!"),
        _raw("gp:1", "Withdrawal is stuck for 5 days, please fix it!!"),  # same id
        _raw("gp:2", "withdrawal  is stuck for 5 days please fix it"),  # same normalized text, other id
        _raw("gp:3", "charts are very slow during market open every day"),
    ]
    out = normalize(raws, SINCE, UNTIL, SALT)
    assert [r.text for r in out] == [
        "Withdrawal is stuck for 5 days, please fix it!!",
        "charts are very slow during market open every day",
    ]


@pytest.mark.parametrize(
    "text,kept",
    [
        ("Good app", False),
        ("nice app for beginners, very easy", False),  # 6 words
        ("app is good but charges are high", False),  # 7 words
        ("app is good but the charges are high", True),  # 8 words
        ("Great app 👍👍👍 👍 👍 👍 👍 👍", False),  # emoji are not words
        ("पैसा नहीं आया अभी तक मेरा withdrawal अटका है", True),  # 8 Devanagari/Latin words
    ],
)
def test_keeps_only_reviews_with_at_least_8_words(text, kept):
    assert len(normalize([_raw("gp:1", text)], SINCE, UNTIL, SALT)) == int(kept)


def test_min_words_is_configurable():
    assert len(normalize([_raw("gp:1", "app is good but charges high")], SINCE, UNTIL, SALT, min_words=3)) == 1


def test_emoji_removed_from_text_and_title():
    raws = [_raw("gp:1", "Great app 👍👍 for SIP investing, very easy to use daily 🙏🏻❤️",
                 title="Love it 😍")]
    (r,) = normalize(raws, SINCE, UNTIL, SALT)
    assert r.text == "Great app for SIP investing, very easy to use daily"
    assert r.title == "Love it"


def test_emoji_removal_keeps_rupee_and_punctuation():
    (r,) = normalize([_raw("gp:1", "brokerage of ₹20 per order is too high — please reduce it 🙄")],
                     SINCE, UNTIL, SALT)
    assert r.text == "brokerage of ₹20 per order is too high — please reduce it"


def test_user_name_never_in_output():
    raws = [_raw("gp:1", "Rahul Sharma here, my withdrawal is stuck since last week", user="Rahul Sharma")]
    out = normalize(raws, SINCE, UNTIL, SALT)
    assert out
    for r in out:
        assert all("Rahul" not in str(field) for field in astuple(r))


def test_text_is_scrubbed():
    raws = [_raw("gp:1", "please call me on 9876543210, or mail a.b@gmail.com today",
                 title="Hi Rahul from support")]
    (r,) = normalize(raws, SINCE, UNTIL, SALT)
    assert r.text == "please call me on [phone], or mail [email] today"
    assert r.title == "Hi [name] from support"


def test_unicode_and_whitespace_normalized():
    raw = "  café   app\n\nis   slow  after the latest update  "
    (r,) = normalize([_raw("gp:1", raw)], SINCE, UNTIL, SALT)
    assert r.text == "café app is slow after the latest update"


def test_reviews_get_language_tag():
    raws = [_raw("gp:1", "The withdrawal has been stuck for five days and support is not replying"),
            _raw("gp:2", "पैसा नहीं आया अभी तक मेरा withdrawal अटका है")]
    assert [r.lang for r in normalize(raws, SINCE, UNTIL, SALT)] == ["en", "hi"]


@pytest.mark.parametrize("text", ["👍👍", "good", "nice app", "withdrawal is stuck for days", "5 5 5 5 5 5 5 5",
                                  "!!! ???", ""])
def test_is_noise_true(text):
    assert is_noise(text)


@pytest.mark.parametrize("text", [LONG, "पैसा नहीं आया अभी तक मेरा withdrawal अटका है"])
def test_is_noise_false(text):
    assert not is_noise(text)


def test_detect_lang_mixed_script():
    assert detect_lang("पैसा नहीं आया withdrawal") == "hi"
    assert detect_lang("🙏🙏🙏") == "und"
    assert detect_lang("") == "und"


@pytest.mark.parametrize(
    "text",  # short English reviews langdetect tagged no/af/fr/cs on real data
    ["Best app for investors", "very better trading", "verry good working", "nice investment app",
     "nice very nice", "response is very good"],
)
def test_detect_lang_short_english_is_en(text):
    assert detect_lang(text) == "en"


@pytest.mark.parametrize(
    "text",  # romanised Hindi from real reviews (langdetect said id/tl/et)
    ["Bhai internet sab thik hai fir bhi ye app open nahi hota", "bahut achcha bahut behtar hai",
     "Paisa khane wala ap hai bas", "scaling ka option achha tha ab use krne me maja nhi aa raha hai"],
)
def test_detect_lang_hinglish_is_hi_latn(text):
    assert detect_lang(text) == "hi-Latn"


def test_detect_lang_long_non_english_latin_text_kept():
    assert detect_lang("Esta aplicación es muy buena para invertir en el mercado de valores y fondos") == "es"


def test_detect_lang_english_and_deterministic():
    text = "The app crashes every time I open the portfolio page after the update"
    assert detect_lang(text) == "en"
    assert {detect_lang(text) for _ in range(5)} == {"en"}


@pytest.mark.parametrize("user,text,expected", [
    ("Pranav Patil", "Pranav Patil, I have an awesome experience with this trading app",
     "[name], I have an awesome experience with this trading app"),
    ("chandan Das", "chandan Das says the charts are slow and the candles are wrong", "[name] says the charts are slow"),
    ("Nishant Kumar", "Thanks from Nishant, withdrawal took five days to reach my bank",
     "Thanks from [name], withdrawal took five days"),
])
def test_reviewer_signing_with_own_name_is_redacted(user, text, expected):
    (r,) = normalize([_raw("gp:1", text, user=user)], SINCE, UNTIL, SALT)
    assert r.text.startswith(expected)
    assert all(part.lower() not in r.text.lower() for part in user.split())


@pytest.mark.parametrize("user", ["Good Investor", "A Google user", "Happy", "Mr X"])
def test_user_names_made_of_common_words_do_not_redact_review_words(user):
    text = "Good app for a happy investor, I use Google pay with it every day"
    (r,) = normalize([_raw("gp:1", text, user=user)], SINCE, UNTIL, SALT)
    assert r.text == text
