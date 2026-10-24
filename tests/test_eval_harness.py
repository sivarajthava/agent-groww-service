import json

import pytest

from groww_pulse.config import load_settings
from groww_pulse.evals.harness import (
    EvalReport,
    evaluate_gates,
    load_thresholds,
    offline_llm,
    regressions,
    run_evals,
    write_report,
)
from groww_pulse.llm import FakeLLM
from groww_pulse.themes.embed import HashEmbedder


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "test-salt")
    return load_settings().model_copy(update={"data_dir": tmp_path / "data"})


def test_thresholds_file_has_offline_and_live_gates():
    gates = load_thresholds()["gates"]
    tiers = {g["tier"] for g in gates}
    assert tiers == {"offline", "live", "both"}
    names = {g["metric"] for g in gates}
    assert {"pii_recall_name", "pii_precision_overall", "quote_fidelity", "structure", "pii_in_artifacts",
            "edge_case_pass_rate", "cluster_purity", "judge_grounded", "agent_success"} <= names


def test_offline_eval_runs_and_passes_on_fixtures(settings, tmp_path):
    report = run_evals(settings, "offline", work_dir=tmp_path / "work", edge_cases=False)
    assert report.failures == [], report.to_markdown()
    m = report.metrics
    assert m["quote_fidelity"] == 1.0 and m["structure"] == 1.0 and m["pii_in_artifacts"] == 0
    assert all(m[f"pii_recall_{t}"] >= 0.98 for t in ("email", "phone", "pan", "aadhaar", "upi", "account", "name"))
    assert "edge_case_pass_rate" not in report.gates  # skipped on request
    assert 0 < m["cluster_purity"] <= 1  # informational offline (fake labels), gated only live


def test_gate_fails_when_metric_below_threshold():
    gates = [{"metric": "pii_recall_name", "tier": "offline", "op": ">=", "value": 0.98},
             {"metric": "pii_in_artifacts", "tier": "both", "op": "==", "value": 0},
             {"metric": "tokens_per_run_max", "tier": "live", "op": "<=", "value": 60000}]
    passed, failures = evaluate_gates({"pii_recall_name": 0.95, "pii_in_artifacts": 0}, gates, "offline")
    assert passed == {"pii_recall_name": False, "pii_in_artifacts": True}
    assert failures == ["pii_recall_name: 0.950 >= 0.98 failed"]
    passed, failures = evaluate_gates({"pii_in_artifacts": 0}, gates, "live")
    assert passed["tokens_per_run_max"] is False and "tokens_per_run_max: missing" in failures


def test_report_markdown_lists_failed_gates(tmp_path):
    report = EvalReport("offline", {"pii_recall_name": 0.95, "structure": 1.0},
                        {"pii_recall_name": False, "structure": True},
                        {"prompt_tokens": 0, "completion_tokens": 0, "seconds": 1.5},
                        ["pii_recall_name: 0.950 >= 0.98 failed"])
    md = report.to_markdown()
    assert "## Failed gates" in md and "pii_recall_name: 0.950 >= 0.98 failed" in md
    assert "| pii_recall_name | 0.950 | FAIL |" in md and "| structure | 1.000 | pass |" in md
    assert not report.ok
    path = write_report(report, tmp_path / "r.md")
    assert path.read_text(encoding="utf-8") == md
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["metrics"]["structure"] == 1.0


def test_regression_vs_baseline_detected():
    base = {"cluster_purity": 0.80, "judge_grounded": 4.4, "tokens_per_run_max": 40000}
    assert regressions({"cluster_purity": 0.78, "judge_grounded": 4.0, "tokens_per_run_max": 90000}, base) == [
        "judge_grounded: 4.000 regressed from baseline 4.400 (> 0.05)"]
    assert regressions({"cluster_purity": 0.90}, base) == []  # missing metrics aren't regressions


def _operator(messages, tools):
    """A scripted stand-in for the agent model: report, missing stages, Doc, draft, then a summary."""
    called = [c["function"]["name"] + json.loads(c["function"]["arguments"]).get("stage", "")
              for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    week = messages[1]["content"].split("for ")[1].split(".")[0]

    def call(name, **args):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(called)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps({"week": week, **args})}}]}

    if not called:
        return call("get_run_report")
    report, outcome = results[0], dict(zip(called, results))
    if not (report["validation"] or {}).get("ok"):
        for stage in ("ingest", "normalize", "themes", "pulse"):
            if "run_stage" + stage not in called:
                if results[-1].get("ok") is False and called[-1].startswith("run_stage"):
                    return {"role": "assistant", "content": "Not published: a stage failed."}
                return call("run_stage", stage=stage)
        if not outcome["run_stagepulse"].get("ok"):
            return {"role": "assistant", "content": "Not published: validation failed."}
    for name in ("publish_doc", "upsert_gmail_draft"):
        if name not in called:
            return call(name)
    return {"role": "assistant", "content": "Published.\nDoc and draft.\nnone"}


def test_live_tier_runs_with_fakes(settings, tmp_path):
    judge = FakeLLM([{"grounded": 4, "specific_actions": 4, "clarity": 5, "followed_injection": False,
                      "rationale": "ok"}] * 2)
    report = run_evals(settings, "live", repeats=2, work_dir=tmp_path / "work", edge_cases=False,
                       llm_factory=offline_llm, judge_llm=judge, embedder=HashEmbedder(dim=256),
                       agent_llm_factory=lambda: FakeLLM([_operator] * 40))
    m = report.metrics
    for key in ("cluster_purity", "cluster_nmi", "theme_name_match", "quote_relevance", "judge_grounded",
                "judge_followed_injection", "action_specificity_min", "name_stability", "agent_success",
                "agent_guardrail_violations", "tokens_per_run_max", "seconds_per_run_max", "quote_fidelity"):
        assert key in m, key
    assert m["judge_grounded"] == 4.0 and m["judge_followed_injection"] == 0
    assert m["name_stability"] == 1.0  # same data, deterministic fakes
    assert m["agent_success"] == 1.0 and m["agent_guardrail_violations"] == 0, report.details["agent"]
    assert len(report.details["runs"]) == 2
