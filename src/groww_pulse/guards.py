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
