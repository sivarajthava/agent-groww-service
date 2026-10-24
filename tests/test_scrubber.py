import pytest

from groww_pulse.privacy.scrubber import REDACTION_TOKENS, find_pii, scrub_text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mail me at a.b@gmail.com", "mail me at [email]"),
        ("call +91 98765 43210 now", "call [phone] now"),
        ("my PAN ABCDE1234F", "my PAN [id]"),
        ("aadhaar 1234 5678 9012", "aadhaar [id]"),
        ("paid to rahul@okaxis", "paid to [id]"),
        ("Hi Rahul from support was rude", "Hi [name] from support was rude"),
        ("withdrawal stuck for 5 days", "withdrawal stuck for 5 days"),  # numbers that aren't PII survive
    ],
)
def test_scrub_text(raw, expected):
    assert scrub_text(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # phones and ids
        ("call 9876543210 asap", "call [phone] asap"),
        ("whatsapp 098765-43210", "whatsapp [phone]"),
        ("reach me on +1 415 555 0134", "reach me on [phone]"),
        ("order id 123456789012345 failed", "order id [id] failed"),
        ("account 50100234567890 blocked", "account [id] blocked"),
        ("lost Rs 25,000 in 3 trades", "lost Rs 25,000 in 3 trades"),
        ("rated 4.5 in 2026", "rated 4.5 in 2026"),
        # names the small NER model misses, caught by context rules
        ("Sachin from customer care helped me", "[name] from customer care helped me"),
        ("I spoke to Priya and she was helpful", "I spoke to [name] and she was helpful"),
        ("Mr. Verma did not help", "Mr. [name] did not help"),
        ("The agent Rahul Sharma was rude", "The agent [name] was rude"),
        # brand / finance / review words the NER model wrongly calls PERSON are kept
        ("Groww app is good", "Groww app is good"),
        ("Good App", "Good App"),
        ("Zerodha is better than Groww", "Zerodha is better than Groww"),
        ("Thank you Groww team", "Thank you Groww team"),
        ("Hello Groww team, fix the app", "Hello Groww team, fix the app"),
        ("My SIP in Nifty 50 failed", "My SIP in Nifty 50 failed"),
        # non-English text passes through untouched
        ("पैसा नहीं आया", "पैसा नहीं आया"),
        # false positives seen on real Groww reviews: Hinglish, lowercase, acronyms, typos, contractions
        ("Groww app ke liye bahut accha hai", "Groww app ke liye bahut accha hai"),
        ("Paisa Kiya withdraw Hai par nahi aaya", "Paisa Kiya withdraw Hai par nahi aaya"),
        ("Grow is the best app", "Grow is the best app"),
        ("SL and MF orders fail, UI is slow", "SL and MF orders fail, UI is slow"),
        ("Easy Interface and Smooth Interface", "Easy Interface and Smooth Interface"),
        ("Hi Don't update the app", "Hi Don't update the app"),
        ("Dear Groww Team, please fix charts", "Dear Groww Team, please fix charts"),
    ],
)
def test_scrub_text_extra_cases(raw, expected):
    assert scrub_text(raw) == expected


def test_scrub_is_idempotent():
    once = scrub_text("Hi Rahul, mail a.b@gmail.com or call 9876543210")
    assert scrub_text(once) == once


def test_find_pii_ignores_tokens():
    assert find_pii("call [phone]") == []
    assert find_pii(" ".join(REDACTION_TOKENS)) == []


def test_find_pii_returns_matched_spans():
    found = find_pii("mail a.b@gmail.com, PAN ABCDE1234F, Hi Rahul")
    assert found == ["a.b@gmail.com", "ABCDE1234F", "Rahul"]


def test_find_pii_clean_text():
    assert find_pii("withdrawal stuck for 5 days, Groww support is slow") == []
