"""PII scrubber: regex rules for contact details and ids, NER plus context rules for person names.

Runs before anything is written to SQLite and again over final artifacts (via find_pii).
"""

import re

import spacy
from spacy.lang.en.stop_words import STOP_WORDS

REDACTION_TOKENS = ("[email]", "[phone]", "[id]", "[name]")

# Rules run in this order; each sees the text left by the previous ones.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)")
_UPI = re.compile(r"\b[\w.-]+@[a-zA-Z]{2,}\b")
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s-]?\d{2,5}){2,5}(?!\d)")
_PHONE_IN = re.compile(r"(?<!\w)(?:0|91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)|(?<!\d)[6-9]\d{2}[\s-]\d{3}[\s-]\d{4}(?!\d)")
_DIGIT_RUN = re.compile(r"(?<!\d)\d{9,18}(?!\d)")

_RULES = (
    (_EMAIL, "[email]"),
    (_PAN, "[id]"),
    (_AADHAAR, "[id]"),
    (_UPI, "[id]"),
    (_PHONE_INTL, "[phone]"),
    (_PHONE_IN, "[phone]"),
    (_DIGIT_RUN, "[id]"),
)

# The small NER model tags brands, finance terms and review words as PERSON ("Groww", "Good App");
# an entity containing any of these words is not a name.
NOT_NAMES = frozenset(
    """
    groww zerodha upstox angel angelone paytm phonepe gpay google play store playstore kite coin dhan fyers
    indmoney kuvera smallcase sbi hdfc icici axis kotak yes nse bse sebi rbi amc nifty sensex banknifty finnifty
    sip sips ipo ipos kyc upi mtf fno etf etfs demat nav mutual fund funds stock stocks share shares trading trade
    android iphone ios app apps application india indian team support customer care service services help
    good nice best great excellent awesome amazing wonderful fantastic superb super bad worst poor useless
    love loved thanks thank hello hi hey dear sir madam please very really easy simple user friendly helpful
    experience platform investment investing investor beginner beginners ok okay okk fine cool wow happy
    grow interface smooth mode wallet chart charts primary smart rest avg overall middle class scalper
    thankyou thanku update features feature option options order orders account money withdrawal
    """.split()
    # Common romanised Hindi (Hinglish) words, which reviews often capitalise.
    + """
    hai hain ho hota hoti hote kiya kiye karo kare karna krna kar kya kyu kyun kab ab abhi jo jab tab sab
    kuch dena diya lena liya tak paisa paise rupay rupaye ke ki ka ko se me mein nahi nhi nahin bhi aur ye yeh
    wo woh bahut bhut bohot bhot accha achha achcha acha bhai ji ek mera meri mere apna apni aap aapka aapki
    hum ham tum iska iski uska uski inka unka liye wala wali raha rahi rahe gaya gayi jata jati chahiye
    chaiye din saal baad pehle phir fir lekin par pe toh hi jaldi dikkat samasya sahi bekar faltu bakwas
    """.split()
)

# Title-case words only, not running into an apostrophe ("Don't").
_NAME = r"[A-Z][a-z]+(?!['’\w])(?:\s+[A-Z][a-z]+(?!['’\w]))?"
# Context rules for names the small model misses ("Hi Rahul", "Sachin from customer care").
_NAME_CONTEXT = (
    re.compile(
        r"(?i:\b(?:hi|hello|hey|dear|mr|mrs|ms|miss|dr|sir|agent|executive|representative|advisor|named|called))"
        rf"\.?,?\s+({_NAME})"
    ),
    re.compile(rf"(?i:\b(?:spoke|spoken|talked|chatted|speaking)\s+(?:to|with))\s+({_NAME})"),
    re.compile(
        rf"\b({_NAME})\s+(?i:from\s+(?:the\s+)?(?:groww\s+)?"
        r"(?:support|customer|care|help|team|service|executive|backend))"
    ),
)

_nlp = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer"])


def _is_name_word(word: str) -> bool:
    w = word.strip(".,")
    return w.istitle() and len(w) > 1 and w.lower() not in NOT_NAMES and w.lower() not in STOP_WORDS


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Shrink a candidate span to its run of name-like words; None if nothing is left."""
    words = [(m.start() + start, m.end() + start, m.group()) for m in re.finditer(r"\S+", text[start:end])]
    while words and not _is_name_word(words[0][2]):
        words.pop(0)
    while words and not _is_name_word(words[-1][2]):
        words.pop()
    if not words or any(not w.strip(".,").isalpha() for _, _, w in words):
        return None
    return words[0][0], words[-1][1]


def _name_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for rule in _NAME_CONTEXT:
        spans += [m.span(1) for m in rule.finditer(text)]
    if any(c.isupper() for c in text):  # names need a capital letter; skips the model for most reviews
        spans += [(e.start_char, e.end_char) for e in _nlp(text).ents if e.label_ == "PERSON"]
    merged: list[tuple[int, int]] = []
    for s, e in sorted(t for sp in spans if (t := _trim(text, *sp))):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def _scrub(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    for rule, token in _RULES:
        found += rule.findall(text)
        text = rule.sub(token, text)
    spans = _name_spans(text)
    found += [text[s:e] for s, e in spans]
    for s, e in reversed(spans):
        text = text[:s] + "[name]" + text[e:]
    return text, found


def scrub_text(text: str) -> str:
    return _scrub(text)[0]


def find_pii(text: str) -> list[str]:
    """Every PII span the scrubber would redact. Redaction tokens themselves never match."""
    return _scrub(text)[1]
