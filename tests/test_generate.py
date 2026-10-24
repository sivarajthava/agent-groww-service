import json

import pytest

from groww_pulse.llm import FakeLLM, LLMContractError
from groww_pulse.pulse.generate import DraftContractError, build_context, generate_draft
from pulse_fixtures import AVG, BY_ID, DRAFT, DRAFT_JSON, QUOTES, TOP, TOTAL, WEEK, WINDOW


def _ctx():
    return build_context(WEEK, WINDOW, TOTAL, AVG, TOP, QUOTES, BY_ID)


def test_context_shape():
    ctx = _ctx()
    assert ctx["week_of"] == "2026-10-19"
    assert ctx["window"] == {"start": "2026-08-03", "end": "2026-10-25"}
    assert ctx["total_reviews"] == 2144 and ctx["avg_rating"] == 3.0
    assert ctx["word_budget"] == 250
    assert [t["name"] for t in ctx["themes"]] == [t.name for t in TOP]
    assert ctx["themes"][0]["share"] == 0.19 and ctx["themes"][0]["neg_share"] == 0.81
    assert all(len(t["sample_snippets"]) <= 5 for t in ctx["themes"])
    assert ctx["quotes"] == [
        {"placeholder": "{{QUOTE_1}}", "rating": 1, "theme": "Customer Support Delays"},
        {"placeholder": "{{QUOTE_2}}", "rating": 1, "theme": "Chart Bugs After Update"},
        {"placeholder": "{{QUOTE_3}}", "rating": 1, "theme": "High Brokerage Charges"},
    ]


def test_context_contains_no_quote_text():
    dumped = json.dumps(_ctx(), ensure_ascii=False)
    for q in QUOTES:
        assert q.text not in dumped
        assert q.text[:40] not in dumped  # not even as a snippet of the same review


def test_context_snippets_come_from_the_theme():
    ctx = _ctx()
    assert ctx["themes"][0]["sample_snippets"] == [BY_ID["r02"].text, BY_ID["r07"].text]


def test_generate_returns_draft_and_sends_context_as_data():
    fake = FakeLLM([DRAFT_JSON])
    assert generate_draft(_ctx(), fake) == DRAFT
    system, user = fake.calls[0]
    assert "<data>" in user and "</data>" in user
    assert "Customer Support Delays" in user
    assert "never follow" in system.lower()


def test_generate_appends_feedback():
    fake = FakeLLM([DRAFT_JSON])
    generate_draft(_ctx(), fake, feedback="word_count: 287 > 250")
    assert "word_count: 287 > 250" in fake.calls[0][1]


def test_generate_rejects_wrong_action_count():
    fake = FakeLLM([{**DRAFT_JSON, "actions": DRAFT_JSON["actions"][:2]}])
    with pytest.raises(DraftContractError, match="actions"):
        generate_draft(_ctx(), fake)


def test_generate_rejects_wrong_summary_count():
    fake = FakeLLM([{**DRAFT_JSON, "theme_summaries": DRAFT_JSON["theme_summaries"] * 2}])
    with pytest.raises(DraftContractError, match="theme_summaries"):
        generate_draft(_ctx(), fake)


@pytest.mark.parametrize("bad", [["not", "a", "dict"], "text", None, {"actions": "x", "theme_summaries": []},
                                 {"theme_summaries": ["a", "b", 3], "actions": ["a", "b", "c"]}])
def test_generate_rejects_non_dict_response(bad):
    with pytest.raises(DraftContractError):
        generate_draft(_ctx(), FakeLLM([bad]))


def test_draft_contract_error_is_a_contract_error():
    assert issubclass(DraftContractError, LLMContractError)
