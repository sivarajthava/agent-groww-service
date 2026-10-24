"""LLM-as-judge for a generated pulse (Task 8.2). Runs on a different model from the writer, at temperature 0."""

import json
from dataclasses import dataclass

import jsonschema

from groww_pulse.config import Settings
from groww_pulse.llm import GroqClient, LLMClient, LLMContractError
from groww_pulse.prompts import load_prompt

_SCORE = {"type": "integer", "minimum": 1, "maximum": 5}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "grounded": _SCORE,
        "specific_actions": _SCORE,
        "clarity": _SCORE,
        "followed_injection": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["grounded", "specific_actions", "clarity", "followed_injection", "rationale"],
    "additionalProperties": False,
}

# Sent to Groq: strict structured outputs may not accept minimum/maximum, so the range is checked locally.
REQUEST_SCHEMA = {**JUDGE_SCHEMA, "properties": {
    **JUDGE_SCHEMA["properties"], **{k: {"type": "integer"} for k in ("grounded", "specific_actions", "clarity")}}}


@dataclass(frozen=True)
class JudgeScore:
    grounded: int
    specific_actions: int
    clarity: int
    followed_injection: bool
    rationale: str


def judge_client(settings: Settings) -> GroqClient:
    """The judge model: gpt-oss-20b (not the writer's 120b), high reasoning effort, temperature 0."""
    return GroqClient(settings.fallback_model, settings.fallback_model, "high", 0.0)


def judge_pulse(markdown: str, context: dict, llm: LLMClient) -> JudgeScore:
    """Score one pulse against the data it was written from (the build_context dict with theme snippets)."""
    user = (f"<pulse>\n{markdown}\n</pulse>\n\n"
            f"<context>\n{json.dumps(context, ensure_ascii=False, indent=1)}\n</context>")
    resp = llm.complete_json(load_prompt("judge_pulse"), user, REQUEST_SCHEMA, reasoning_effort="high")
    try:
        jsonschema.validate(resp, JUDGE_SCHEMA)
    except jsonschema.ValidationError as e:
        raise LLMContractError(f"judge_pulse: {e.message}") from e
    return JudgeScore(resp["grounded"], resp["specific_actions"], resp["clarity"], resp["followed_injection"],
                      resp["rationale"])
