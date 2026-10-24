"""Pulse validators (architecture.md §4.8) and the bounded generate → render → validate loop."""

import re
from dataclasses import replace

from groww_pulse.llm import LLMClient
from groww_pulse.models import Pulse, PulseDraft, Review, ValidationResult
from groww_pulse.privacy.scrubber import REDACTION_TOKENS, find_pattern_pii, find_pii
from groww_pulse.pulse.generate import DraftContractError, generate_draft
from groww_pulse.pulse.render import render_markdown

HARD_CHECKS = ("quote_fidelity", "themes", "quotes", "pii", "banned")  # never retried: the LLM can't fix them
SUMMARY_FALLBACK_WORDS = 12
# Fixed template text, masked before the NER scan ("What Users Said" reads as a PERSON to the small model).
TEMPLATE_TEXT = ("Groww — Weekly Review Pulse", "Week of", "Window:", "Reviews analyzed:", "Avg rating:",
                 "Top Themes", "What Users Said", "Action Ideas")
_URL = re.compile(r"https?://\S+")
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{64}\b")
_REDACTION_SPLIT = re.compile("|".join(re.escape(t) for t in REDACTION_TOKENS))


class ValidationFailed(Exception):
    def __init__(self, result: ValidationResult):
        super().__init__("; ".join(result.failures))
        self.result = result


def word_count(markdown: str) -> int:
    """Words in the rendered pulse, ignoring Markdown syntax; "★1" and "19%" count as one word each."""
    text = re.sub(r"(?m)^\s*(#+|[-*]|\d+\.)\s+", " ", markdown).replace("*", " ")
    return len(re.findall(r"[\w’'-]+", text))


def _quote_ok(text: str, review: Review | None) -> bool:
    if review is None:
        return False
    pieces = [p.strip() for p in _REDACTION_SPLIT.split(text.replace("…", ""))]
    return any(pieces) and all(p in review.text for p in pieces if p)


def _pii_spans(pulse: Pulse) -> list[str]:
    masked = pulse.markdown
    for t in pulse.themes:  # LLM-chosen Title-Case labels; checked with patterns below instead
        masked = masked.replace(t.name, "theme")
    for literal in TEMPLATE_TEXT:
        masked = masked.replace(literal, " ")
    found = [span for line in masked.splitlines() for span in find_pii(line)]
    return found + [span for t in pulse.themes for span in find_pattern_pii(t.name)]


def validate(
    pulse: Pulse, reviews_by_id: dict[str, Review], max_words: int = 250, doc_url: str | None = None
) -> ValidationResult:
    """Every check from architecture.md §4.8. Failure messages name the check and never echo PII values."""
    failures: list[str] = []
    md = pulse.markdown

    for i, q in enumerate(pulse.quotes, 1):
        if not _quote_ok(q.text, reviews_by_id.get(q.review_id)):
            failures.append(f"quote_fidelity: quote {i} is not verbatim review text")
        elif f'"{q.text}"' not in md:
            failures.append(f"quote_fidelity: quote {i} missing from the pulse")

    for name, items in (("themes", pulse.themes), ("quotes", pulse.quotes), ("actions", pulse.draft.actions)):
        if len(items) != 3:
            failures.append(f"{name}: {len(items)} != 3")

    names = [t.name.lower() for t in pulse.themes]
    for i, action in enumerate(pulse.draft.actions, 1):
        if not any(n in action.lower() for n in names):
            failures.append(f"actions: action {i} mentions no top theme")

    words = word_count(md)
    if words > max_words:
        failures.append(f"word_count: {words} > {max_words}")

    spans = _pii_spans(pulse)
    if spans:
        failures.append(f"pii: {len(spans)} span(s) found in the pulse")

    urls = [u for u in _URL.findall(md) if not doc_url or u.rstrip(".,)") != doc_url]
    if urls:
        failures.append(f"banned: {len(urls)} URL(s) other than the Doc link")
    if _HEX_ID.search(md):
        failures.append("banned: review id in the pulse")

    return ValidationResult(ok=not failures, failures=tuple(failures))


def _is_hard(result: ValidationResult) -> bool:
    return any(f.split(":")[0] in HARD_CHECKS for f in result.failures)


def _cut(summary: str, n: int = SUMMARY_FALLBACK_WORDS) -> str:
    words = summary.split()
    return summary if len(words) <= n else " ".join(words[:n]).rstrip(".,;:") + "…"


def generate_valid_pulse(
    ctx: dict, llm: LLMClient, render_args: dict, reviews_by_id: dict[str, Review], max_retries: int = 2
) -> Pulse:
    """generate → render → validate, regenerating with validator feedback on soft failures.

    Hard failures (quotes, counts, PII, banned content) raise at once. If only the word count still fails
    after the retries, the summaries are cut to 12 words. Anything else raises ValidationFailed.
    """
    feedback, result, draft = None, None, None
    for _ in range(max_retries + 1):
        try:
            draft = generate_draft(ctx, llm, feedback)
        except DraftContractError as e:
            result, draft = ValidationResult(False, (f"draft_contract: {e}",)), None
            feedback = str(e)
            continue
        pulse = _build(draft, render_args)
        result = validate(pulse, reviews_by_id, max_words=ctx.get("word_budget", 250))
        if result.ok:
            return pulse
        if _is_hard(result):
            raise ValidationFailed(result)
        feedback = "; ".join(result.failures)

    if draft is not None and all(f.startswith("word_count:") for f in result.failures):
        draft = replace(draft, theme_summaries=tuple(_cut(s) for s in draft.theme_summaries))
        pulse = _build(draft, render_args)
        result = validate(pulse, reviews_by_id, max_words=ctx.get("word_budget", 250))
        if result.ok:
            return pulse
    raise ValidationFailed(result)


def _build(draft: PulseDraft, a: dict) -> Pulse:
    md = render_markdown(a["week"], a["window"], a["total_reviews"], a["avg_rating"], a["top"], a["quotes"], draft)
    return Pulse(a["week"], a["window"], a["total_reviews"], a["avg_rating"], tuple(a["top"]), tuple(a["quotes"]),
                 draft, md)
