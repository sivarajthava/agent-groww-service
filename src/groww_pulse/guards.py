"""Checks on LLM output against prompt injection (Task 8.4). A hit is a contract failure, so the call is retried."""

import re

# Links in any form: schemes, www., or a bare domain with a common TLD (".example" is used by the eval data).
# An email's domain is not a link: emails are PII, a hard validator failure rather than a retried contract one.
URL_LIKE = re.compile(r"https?://|\bwww\.|(?<![@\w.-])[\w-]+\.(?:com|in|net|org|io|co|app|ly|me|example)\b",
                      re.IGNORECASE)
# Instruction verbs that never belong in a theme name; their presence means the model echoed a review's command.
IMPERATIVE = re.compile(r"\b(?:ignore|disregard|forget|click|visit|follow|write|say|tell|announce|pretend|"
                        r"respond|output|label|rate|list|give|delete|include)\b", re.IGNORECASE)
MAX_NAME_WORDS = 5


def has_url(text: str) -> bool:
    return bool(URL_LIKE.search(text))


# Review text that carries markup or reads as instructions to a model never becomes a quote.
MARKUP = re.compile(r"<\s*/?\s*[a-z!]|\*\*|__|\]\(|`")
INJECTION_LIKE = re.compile(
    r"\b(?:ignore|disregard|forget)\b.{0,40}\b(?:instructions?|rules?|above|format|limits?)\b|\bsystem\s*:|"
    r"\byou are now\b|\bpretend (?:you are|to be)\b|\brespond in\b|\bin (?:your|the) (?:summary|report|output)\b|"
    r"\b(?:give|list|write)\s+(?:five|5|ten|10)\b|\blabel every\b|\breturn an? (?:empty )?json\b",
    re.IGNORECASE,
)


def unsafe_for_quote(text: str) -> bool:
    return has_url(text) or bool(MARKUP.search(text) or INJECTION_LIKE.search(text))
