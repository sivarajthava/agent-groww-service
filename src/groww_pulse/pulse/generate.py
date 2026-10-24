"""LLM draft of the pulse: theme summaries and action ideas only. Quote text is never sent to the LLM."""

import json
from datetime import date

from groww_pulse.llm import LLMClient, LLMContractError
from groww_pulse.models import PulseDraft, Quote, Review, Theme
from groww_pulse.prompts import load_prompt
from groww_pulse.themes.quotes import trim_to_span
from groww_pulse.weeks import week_window

SNIPPETS_PER_THEME = 5
SNIPPET_WORDS = 30

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "theme_summaries": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["theme_summaries", "actions"],
    "additionalProperties": False,
}


class DraftContractError(LLMContractError):
    """The draft didn't have exactly 3 summaries and 3 actions, all strings."""


def week_monday(week: str) -> date:
    """Monday of an ISO week string ("2026-W43" -> 2026-10-19), shown as "Week of" in the pulse."""
    return week_window(week, 1)[0]


def build_context(
    week: str,
    window: tuple[date, date],
    total_reviews: int,
    avg_rating: float,
    top: list[Theme],
    quotes: list[Quote],
    reviews_by_id: dict[str, Review],
    word_budget: int = 250,
) -> dict:
    """The only input the pulse LLM sees. Quotes appear as placeholders with ratings, never as text."""
    quote_ids = {q.review_id for q in quotes}
    quote_texts = [q.text.rstrip("…") for q in quotes]
    themes = []
    for t in top:
        snippets = []
        for rid in t.review_ids:
            r = reviews_by_id.get(rid)
            if r is None or rid in quote_ids or r.lang != "en":
                continue
            snippet = trim_to_span(r.text, SNIPPET_WORDS)
            if any(q in r.text or snippet.rstrip("…") in q for q in quote_texts):
                continue
            snippets.append(snippet)
            if len(snippets) == SNIPPETS_PER_THEME:
                break
        themes.append(
            {
                "name": t.name,
                "share": round(t.share, 2),
                "neg_share": round(t.neg_share, 2),
                "description": t.description,
                "sample_snippets": snippets,
            }
        )
    return {
        "week_of": week_monday(week).isoformat(),
        "window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "total_reviews": total_reviews,
        "avg_rating": round(avg_rating, 1),
        "themes": themes,
        "quotes": [
            {"placeholder": f"{{{{QUOTE_{i}}}}}", "rating": q.rating, "theme": next(
                (t.name for t in top if t.id == q.theme_id), "")}
            for i, q in enumerate(quotes, 1)
        ],
        "word_budget": word_budget,
    }


def _three_strings(resp: dict, key: str) -> tuple[str, str, str]:
    items = resp.get(key)
    if not isinstance(items, list) or len(items) != 3 or not all(isinstance(s, str) and s.strip() for s in items):
        got = len(items) if isinstance(items, list) else type(items).__name__
        raise DraftContractError(f"{key}: expected 3 non-empty strings, got {got}")
    return tuple(s.strip() for s in items)


def generate_draft(ctx: dict, llm: LLMClient, feedback: str | None = None) -> PulseDraft:
    user = f"<data>\n{json.dumps(ctx, ensure_ascii=False, indent=1)}\n</data>"
    if feedback:
        user += f"\n\nYour previous draft failed these checks; fix them: {feedback}"
    resp = llm.complete_json(load_prompt("write_pulse"), user, DRAFT_SCHEMA)
    if not isinstance(resp, dict):
        raise DraftContractError(f"expected a JSON object, got {type(resp).__name__}")
    return PulseDraft(theme_summaries=_three_strings(resp, "theme_summaries"), actions=_three_strings(resp, "actions"))
