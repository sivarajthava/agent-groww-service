import json
from collections import Counter
from pathlib import Path

import jsonschema
import pytest

from groww_pulse.privacy.scrubber import find_pii
from groww_pulse.prompts import load_prompt
from groww_pulse.themes.taxonomy import OTHER, TAXONOMY

DATASETS = Path(__file__).parent.parent / "evals" / "datasets"
PII_TYPES = ["email", "phone", "pan", "aadhaar", "upi", "account", "name"]
REVIEW = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "rating": {"type": "integer", "minimum": 1, "maximum": 5},
        "text": {"type": "string", "minLength": 1},
        "date": {"type": "string", "format": "date", "pattern": r"^2026-\d{2}-\d{2}$"},
        "lang": {"type": "string", "minLength": 2},
        "gold_theme": {"enum": [*TAXONOMY, OTHER]},
    },
    "required": ["id", "rating", "text", "date", "lang", "gold_theme"],
}
INJECTION = {**REVIEW, "properties": {**REVIEW["properties"], "injection": {"type": ["string", "null"]}},
             "required": [*REVIEW["required"], "injection"]}
PII = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "spans": {"type": "array", "items": {
            "type": "object",
            "properties": {"start": {"type": "integer", "minimum": 0}, "end": {"type": "integer"},
                           "type": {"enum": PII_TYPES}},
            "required": ["start", "end", "type"], "additionalProperties": False}},
    },
    "required": ["text", "spans"],
}


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (DATASETS / name).read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("name, schema, size", [("reviews_labeled.jsonl", REVIEW, (280, 330)),
                                                ("pii_seeded.jsonl", PII, (180, 230)),
                                                ("injection.jsonl", INJECTION, (35, 50))])
def test_datasets_parse_and_schema_valid(name, schema, size):
    rows = load(name)
    assert size[0] <= len(rows) <= size[1]
    for row in rows:
        jsonschema.validate(row, schema)
    if "id" in schema["properties"]:
        assert len({r["id"] for r in rows}) == len(rows)


def test_labeled_set_has_all_taxonomy_themes():
    rows = load("reviews_labeled.jsonl")
    themes = Counter(r["gold_theme"] for r in rows)
    assert set(TAXONOMY) <= set(themes)
    assert 0.30 <= themes.most_common(1)[0][1] / len(rows) <= 0.40  # one dominant theme, uneven on purpose
    langs = Counter(r["lang"] for r in rows)
    assert 0.08 <= (langs["hi"] + langs["hi-Latn"]) / len(rows) <= 0.12
    noise = [r for r in rows if len(r["text"].split()) <= 2]
    assert 0.04 <= len(noise) / len(rows) <= 0.07 and all(r["gold_theme"] == OTHER for r in noise)
    assert sum(r["id"].startswith("dup") for r in rows) / len(rows) >= 0.04


def test_taxonomy_matches_the_prompt_hints():
    for name in ("label_themes", "merge_themes"):
        assert all(t in load_prompt(name) for t in TAXONOMY), name


def test_pii_spans_point_at_text():
    rows = load("pii_seeded.jsonl")
    for r in rows:
        for s in r["spans"]:
            assert 0 <= s["start"] < s["end"] <= len(r["text"]) and r["text"][s["start"]:s["end"]].strip()
    types = Counter(s["type"] for r in rows for s in r["spans"])
    assert set(types) == set(PII_TYPES) and min(types.values()) >= 10
    assert sum(not r["spans"] for r in rows) >= 30  # negatives that must survive scrubbing


def test_pii_set_covers_the_listed_formats():
    texts = " ".join(r["text"] for r in load("pii_seeded.jsonl"))
    for fmt in ("+91 98", "-", "@ok", "₹", "version 7.12.3", "5 days", "2 hours"):
        assert fmt in texts, fmt


def test_labeled_set_contains_no_pii():
    leaks = {r["id"]: find_pii(r["text"]) for r in load("reviews_labeled.jsonl")}
    assert {k: v for k, v in leaks.items() if v} == {}


def test_injection_set_mixes_instructions_with_normal_reviews():
    rows = load("injection.jsonl")
    kinds = Counter(r["injection"] for r in rows)
    assert sum(v for k, v in kinds.items() if k) >= 18 and kinds[None] >= 15
    assert {"ignore_rules", "add_url", "write_hindi", "name_person", "five_actions"} <= set(kinds)
