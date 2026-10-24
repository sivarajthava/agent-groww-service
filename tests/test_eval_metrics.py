import pytest

from groww_pulse.evals.judge import JUDGE_SCHEMA, JudgeScore, judge_pulse
from groww_pulse.evals.metrics import (
    action_specificity_heuristic,
    cluster_scores,
    gold_majority,
    name_stability,
    pii_scores,
    quote_relevance,
    theme_name_match,
)
from groww_pulse.llm import FakeLLM, LLMContractError
from groww_pulse.models import Quote, Theme
from groww_pulse.themes.embed import HashEmbedder


def span(s, e, t=None):
    return {"start": s, "end": e, "type": t}


def test_pii_scores_perfect_and_partial():
    gold = [[span(0, 10, "phone")], [span(5, 9, "name"), span(20, 30, "email")], []]
    perfect = pii_scores([[span(0, 10, "phone")], [span(5, 9, "name"), span(20, 30, "email")], []], gold)
    assert perfect["overall"] == {"precision": 1.0, "recall": 1.0}
    assert perfect["phone"] == {"precision": 1.0, "recall": 1.0}

    partial = pii_scores([[span(0, 4, "phone")],                 # covers 40% of the gold phone -> miss
                          [span(5, 8, "name")],                  # 75% of the name -> hit; email missed
                          [span(0, 3, "account")]], gold)        # a false positive on a negative
    assert partial["phone"]["recall"] == 0.0 and partial["name"]["recall"] == 1.0
    assert partial["email"]["recall"] == 0.0
    assert partial["overall"]["recall"] == pytest.approx(1 / 3)
    assert partial["overall"]["precision"] == pytest.approx(1 / 3)
    assert partial["account"]["precision"] == 0.0


def test_purity_and_nmi_known_values():
    gold = {"a": "x", "b": "x", "c": "y", "d": "y"}
    assert cluster_scores({"a": 0, "b": 0, "c": 1, "d": 1}, gold) == {"purity": 1.0, "nmi": pytest.approx(1.0)}
    mixed = cluster_scores({"a": 0, "b": 1, "c": 0, "d": 1}, gold)
    assert mixed["purity"] == 0.5 and mixed["nmi"] == pytest.approx(0.0, abs=1e-9)
    assert cluster_scores({"a": 0, "b": 0, "c": 0, "d": 1, "zz": 2}, gold)["purity"] == 0.75  # ungraded ids ignored


def test_name_stability_jaccard():
    assert name_stability(["A", "B", "C"], ["A", "B", "C"]) == 1.0
    assert name_stability(["A", "B", "C"], ["A", "B", "D"]) == 0.5
    assert name_stability([], []) == 1.0


def _theme(tid, name, ids):
    return Theme(tid, name, "d", tuple(ids), len(ids), 0.3, 2.0, 0.5, None)


def test_gold_majority_and_quote_relevance():
    gold = {"r1": "Customer Support", "r2": "Customer Support", "r3": "Charges & Fees", "r4": "Charges & Fees"}
    themes = [_theme("support", "Slow Support", ["r1", "r2", "r3"]), _theme("fees", "High Fees", ["r4"])]
    majority = gold_majority(themes, gold)
    assert majority == {"support": "Customer Support", "fees": "Charges & Fees"}
    quotes = [Quote("q", 1, "r1", "support"), Quote("q", 1, "r3", "support"), Quote("q", 1, "r4", "fees")]
    assert quote_relevance(quotes, gold, majority) == pytest.approx(2 / 3)


def test_theme_name_match_uses_nearest_label():
    themes = [_theme("a", "Customer Support Delays", ["r1"]), _theme("b", "Charges Fees Hidden", ["r2"])]
    assert theme_name_match(themes, {"a": "Customer Support", "b": "Charges & Fees"}, HashEmbedder(dim=512)) == 1.0
    assert theme_name_match(themes, {"a": "Charges & Fees", "b": "Charges & Fees"}, HashEmbedder(dim=512)) == 0.5


def test_action_specificity_heuristic():
    assert action_specificity_heuristic("Withdrawal Delays: cut settlement SLA to 24h")
    assert not action_specificity_heuristic("Improve the app")
    assert not action_specificity_heuristic("Withdrawal Delays: look into it and make things better")
    assert action_specificity_heuristic("Slow Support: reply to every ticket within 2 days", ["Slow Support"])
    assert not action_specificity_heuristic("Reply to every ticket within 2 days", ["Slow Support"])
    assert action_specificity_heuristic("App Crashes: track the crash-free rate weekly and fix the top 5 crashes")


def test_judge_parses_schema_with_fake_llm():
    fake = FakeLLM([{"grounded": 5, "specific_actions": 4, "clarity": 5, "followed_injection": False,
                     "rationale": "Summaries match the snippets."}])
    score = judge_pulse("# Pulse", {"themes": [{"name": "A", "sample_snippets": ["x"]}]}, fake)
    assert score == JudgeScore(5, 4, 5, False, "Summaries match the snippets.")
    system, user = fake.calls[0]
    assert "<pulse>" in user and "<context>" in user and "1" in system and "5" in system
    assert fake.efforts == ["high"]
    assert JUDGE_SCHEMA["properties"]["grounded"] == {"type": "integer", "minimum": 1, "maximum": 5}


@pytest.mark.parametrize("bad", [{"grounded": 7, "specific_actions": 4, "clarity": 5, "followed_injection": False,
                                  "rationale": ""}, ["not", "a", "dict"]])
def test_judge_rejects_out_of_range_scores(bad):
    with pytest.raises(LLMContractError):
        judge_pulse("# Pulse", {}, FakeLLM([bad]))
