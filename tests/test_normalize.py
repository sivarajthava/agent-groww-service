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


def test_id_is_salted_hash_of_source_id():
    (r,) = normalize([_raw("gp:1", "withdrawal is stuck for days")], SINCE, UNTIL, SALT)
    assert r.id == hashlib.sha256(f"{SALT}play_storegp:1".encode()).hexdigest()
    assert r.source == "play_store"
    assert r.date == date(2026, 10, 1)
    assert r.rating == 2 and r.app_version == "15.2.0"


def test_drops_out_of_window():
    raws = [
        _raw("gp:1", "too old to keep in window", at=datetime(2026, 8, 16, 23, 59)),
        _raw("gp:2", "first day of the window", at=datetime(2026, 8, 17, 0, 0)),
        _raw("gp:3", "last day of the window", at=datetime(2026, 10, 25, 23, 59)),
        _raw("gp:4", "too new to keep in window", at=datetime(2026, 10, 26, 0, 0)),
    ]
    assert [r.text for r in normalize(raws, SINCE, UNTIL, SALT)] == ["first day of the window", "last day of the window"]


def test_dedups_by_id_and_text():
    raws = [
        _raw("gp:1", "Withdrawal is stuck for 5 days!!"),
        _raw("gp:1", "Withdrawal is stuck for 5 days!!"),  # same id
        _raw("gp:2", "withdrawal  is stuck for 5 days"),  # same normalized text, other id (copy-paste)
        _raw("gp:3", "charts are slow during market open"),
    ]
    out = normalize(raws, SINCE, UNTIL, SALT)
    assert [r.text for r in out] == ["Withdrawal is stuck for 5 days!!", "charts are slow during market open"]


def test_short_noise_duplicates_are_kept_for_counts():
    raws = [_raw(f"gp:{i}", "Good app", rating=5) for i in range(3)]
    assert len(normalize(raws, SINCE, UNTIL, SALT)) == 3


def test_user_name_never_in_output():
    raws = [_raw("gp:1", "Rahul Sharma here, withdrawal is stuck", user="Rahul Sharma")]
    for r in normalize(raws, SINCE, UNTIL, SALT):
        assert all("Rahul" not in str(field) for field in astuple(r))


def test_text_is_scrubbed():
    raws = [_raw("gp:1", "call me on 9876543210, mail a.b@gmail.com", title="Hi Rahul from support")]
    (r,) = normalize(raws, SINCE, UNTIL, SALT)
    assert r.text == "call me on [phone], mail [email]"
    assert r.title == "Hi [name] from support"


def test_unicode_and_whitespace_normalized():
    (r,) = normalize([_raw("gp:1", "  café   app\n\nis   slow  ")], SINCE, UNTIL, SALT)
    assert r.text == "café app is slow"


def test_reviews_get_language_tag():
    raws = [_raw("gp:1", "The withdrawal has been stuck for five days and support is not replying"),
            _raw("gp:2", "पैसा नहीं आया अभी तक withdrawal")]
    assert [r.lang for r in normalize(raws, SINCE, UNTIL, SALT)] == ["en", "hi"]


@pytest.mark.parametrize("text", ["👍👍", "good", "nice app", "5 5 5", "!!! ???", ""])
def test_is_noise_true(text):
    assert is_noise(text)


@pytest.mark.parametrize("text", ["withdrawal is stuck for days", "पैसा नहीं आया अभी"])
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
