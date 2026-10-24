from dataclasses import replace
from pathlib import Path

import pytest

from groww_pulse.llm import FakeLLM
from groww_pulse.models import Pulse, PulseDraft
from groww_pulse.pulse.generate import build_context
from groww_pulse.pulse.render import render_markdown
from groww_pulse.pulse.validate import ValidationFailed, generate_valid_pulse, validate, word_count
from pulse_fixtures import AVG, BY_ID, DRAFT, DRAFT_JSON, QUOTES, RENDER_ARGS, TOP, TOTAL, WEEK, WINDOW

GOLDEN = Path(__file__).parent / "fixtures" / "golden_pulse.md"


def _pulse(draft=DRAFT, quotes=QUOTES, top=TOP, markdown=None):
    md = markdown if markdown is not None else render_markdown(**{**RENDER_ARGS, "quotes": quotes, "top": top},
                                                               draft=draft)
    return Pulse(WEEK, WINDOW, TOTAL, AVG, tuple(top), tuple(quotes), draft, md)


def _draft(summaries=DRAFT.theme_summaries, actions=DRAFT.actions):
    return PulseDraft(tuple(summaries), tuple(actions))


def _checks(result):
    return {f.split(":")[0] for f in result.failures}


# --- word_count ------------------------------------------------------------------------------


def test_word_count_simple():
    assert word_count("Withdrawal stuck for 5 days") == 5


def test_word_count_strips_markdown_and_counts_stars_and_contractions():
    assert word_count("# Top Themes\n\n1. App — 19% — it's slow\n- \"Bad app\" (★1)\n**bold** text") == 11


def test_golden_is_within_budget():
    assert word_count(GOLDEN.read_text(encoding="utf-8")) <= 250


# --- validate --------------------------------------------------------------------------------


def test_validate_ok_on_golden():
    pulse = _pulse(markdown=GOLDEN.read_text(encoding="utf-8"))
    result = validate(pulse, BY_ID)
    assert result.ok, result.failures
    assert result.failures == ()


def test_detects_tampered_quote():
    q0 = replace(QUOTES[0], text=QUOTES[0].text.replace("ten days", "eleven days"))
    result = validate(_pulse(quotes=[q0, *QUOTES[1:]]), BY_ID)
    assert not result.ok
    assert any(f.startswith("quote_fidelity:") for f in result.failures)


def test_detects_quote_missing_from_markdown():
    md = render_markdown(**RENDER_ARGS, draft=DRAFT).replace(QUOTES[1].text, "Something else entirely here")
    assert "quote_fidelity" in _checks(validate(_pulse(markdown=md), BY_ID))


def test_redacted_quote_passes_fidelity():
    q = replace(QUOTES[0], text="Please call me on [phone] about the refund…", review_id="r07")
    result = validate(_pulse(quotes=[q, *QUOTES[1:]]), BY_ID)
    assert result.ok, result.failures


def test_trimmed_quote_passes_fidelity():
    q = replace(QUOTES[0], text="Customer care never replies to my ticket.…")
    assert validate(_pulse(quotes=[q, *QUOTES[1:]]), BY_ID).ok


def test_detects_pii_in_summary():
    draft = _draft(summaries=("Contact a.b@gmail.com or 9876543210 for refunds.", *DRAFT.theme_summaries[1:]))
    result = validate(_pulse(draft=draft), BY_ID)
    assert "pii" in _checks(result)
    assert all("gmail" not in f and "98765" not in f for f in result.failures)  # values never echoed


def test_detects_person_name_in_action():
    actions = ("Customer Support Delays: Hi Rahul from support should call users back daily.", *DRAFT.actions[1:])
    assert "pii" in _checks(validate(_pulse(draft=_draft(actions=actions)), BY_ID))


def test_theme_names_and_template_text_are_not_pii():
    top = [replace(TOP[0], name="Scalper Mode Complaints"), replace(TOP[1], name="Chart Bug After Update"), TOP[2]]
    actions = ("Scalper Mode Complaints: make scalper mode opt-in and track complaints weekly.",
               "Chart Bug After Update: ship a hotfix restoring the back button this sprint.", DRAFT.actions[2])
    result = validate(_pulse(top=top, draft=_draft(actions=actions)), BY_ID)
    assert result.ok, result.failures


def test_over_250_words_fails():
    long = " ".join(["slow"] * 90)
    draft = _draft(summaries=(long, long, long))
    result = validate(_pulse(draft=draft), BY_ID)
    assert any(f.startswith("word_count:") and "> 250" in f for f in result.failures)


def test_action_must_mention_a_top_theme():
    actions = ("Improve things generally across the app.", *DRAFT.actions[1:])
    result = validate(_pulse(draft=_draft(actions=actions)), BY_ID)
    assert "actions: action 1 mentions no top theme" in result.failures


def test_counts_must_be_three():
    result = validate(_pulse(top=TOP[:2], quotes=QUOTES[:2]), BY_ID)
    assert {"themes", "quotes"} <= _checks(result)


@pytest.mark.parametrize("bad", ["see https://example.com/profile/123", "id " + "a1" * 32])
def test_banned_urls_and_review_ids(bad):
    draft = _draft(summaries=(f"Users complain, {bad}.", *DRAFT.theme_summaries[1:]))
    assert "banned" in _checks(validate(_pulse(draft=draft), BY_ID))


def test_doc_url_is_allowed():
    md = render_markdown(**RENDER_ARGS, draft=DRAFT) + "\nDoc: https://docs.google.com/document/d/abc\n"
    assert validate(_pulse(markdown=md), BY_ID, doc_url="https://docs.google.com/document/d/abc").ok


# --- generate_valid_pulse --------------------------------------------------------------------


def _ctx():
    return build_context(WEEK, WINDOW, TOTAL, AVG, TOP, QUOTES, BY_ID)


LONG_DRAFT = {**DRAFT_JSON, "theme_summaries": [" ".join(["slow"] * 90)] * 3}
BAD_ACTIONS = {**DRAFT_JSON, "actions": ["Do better.", "Fix stuff.", "Improve."]}


def test_retry_then_success():
    fake = FakeLLM([LONG_DRAFT, DRAFT_JSON])
    pulse = generate_valid_pulse(_ctx(), fake, RENDER_ARGS, BY_ID)
    assert pulse.draft == DRAFT
    assert pulse.markdown == GOLDEN.read_text(encoding="utf-8")
    assert len(fake.calls) == 2
    assert "word_count" in fake.calls[1][1]  # validator feedback reached the second prompt


def test_aborts_after_retries():
    fake = FakeLLM([BAD_ACTIONS] * 3)
    with pytest.raises(ValidationFailed) as e:
        generate_valid_pulse(_ctx(), fake, RENDER_ARGS, BY_ID)
    assert len(fake.calls) == 3
    assert any(f.startswith("actions:") for f in e.value.result.failures)


def test_aborts_after_repeated_contract_errors():
    fake = FakeLLM([{"actions": ["a", "b"], "theme_summaries": ["x"]}] * 3)
    with pytest.raises(ValidationFailed, match="draft_contract"):
        generate_valid_pulse(_ctx(), fake, RENDER_ARGS, BY_ID)
    assert len(fake.calls) == 3


def test_word_count_still_failing_truncates_summaries():
    fake = FakeLLM([LONG_DRAFT] * 3)
    pulse = generate_valid_pulse(_ctx(), fake, RENDER_ARGS, BY_ID)
    assert all(len(s.rstrip("…").split()) <= 12 for s in pulse.draft.theme_summaries)
    assert validate(pulse, BY_ID).ok


def test_hard_failure_raises_without_retry():
    q0 = replace(QUOTES[0], text="A quote nobody ever wrote in any stored review at all")
    fake = FakeLLM([DRAFT_JSON, DRAFT_JSON])
    with pytest.raises(ValidationFailed, match="quote_fidelity"):
        generate_valid_pulse(_ctx(), fake, {**RENDER_ARGS, "quotes": [q0, *QUOTES[1:]]}, BY_ID)
    assert len(fake.calls) == 1
