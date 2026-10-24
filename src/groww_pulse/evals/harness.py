"""Eval harness (implementation plan, Task 8.3): run the pipeline over the labeled datasets and gate the metrics.

Offline tier: fakes only (FakeLLM labeler/writer, HashEmbedder by default), no network or secrets. It gates the
PII scrubber, quote fidelity, structure, PII in artifacts and the edge-case suite. Live tier: real Groq writer,
MiniLM and a gpt-oss-20b judge, repeated `repeats` times; it adds theme quality, judge scores, stability, cost
and five agent scenarios against the in-memory MCP server. Thresholds live in evals/thresholds.yaml.
"""

import asyncio
import json
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Literal

import yaml

from groww_pulse.agent.loop import run_agent
from groww_pulse.agent.tools import AgentTools
from groww_pulse.config import Settings
from groww_pulse.evals.fake_mcp import fake_session_factory, make_fake_server
from groww_pulse.evals.judge import judge_client, judge_pulse
from groww_pulse.evals.metrics import (
    action_specificity_heuristic,
    cluster_scores,
    gold_majority,
    name_stability,
    pii_scores,
    quote_relevance,
    theme_name_match,
)
from groww_pulse.llm import FakeLLM, GroqClient, LLMContractError
from groww_pulse.models import Quote, RawReview
from groww_pulse.normalize.normalize import review_id
from groww_pulse.privacy.scrubber import find_pattern_pii, pii_spans
from groww_pulse.publish.mcp_client import McpGateway
from groww_pulse.pulse.validate import _quote_ok
from groww_pulse.runner import RunContext, RunDeps, run_pipeline
from groww_pulse.stages import db_path, run_dir
from groww_pulse.storage.db import Database
from groww_pulse.themes.embed import HashEmbedder
from groww_pulse.weeks import week_window

REPO = Path(__file__).resolve().parents[3]
DATASETS = REPO / "evals" / "datasets"
REPORTS = REPO / "evals" / "reports"
THRESHOLDS = REPO / "evals" / "thresholds.yaml"
BASELINE = REPORTS / "baseline.json"
EVAL_WEEK, EVAL_TODAY = "2026-W43", date(2026, 10, 26)  # the datasets' dates fall in this week's 10-week window
PII_TYPES = ("email", "phone", "pan", "aadhaar", "upi", "account", "name")
# Live metrics where a drop against the baseline is a regression.
HIGHER_IS_BETTER = ("cluster_purity", "cluster_nmi", "theme_name_match", "quote_relevance", "judge_grounded",
                    "judge_specific_actions", "judge_clarity", "action_specificity_min", "name_stability",
                    "agent_success")
_HASH_OR_FLOAT = re.compile(r"\b[0-9a-f]{64}\b|\d+\.\d+")


@dataclass
class EvalReport:
    tier: str
    metrics: dict[str, float]
    gates: dict[str, bool]
    cost: dict[str, float]
    failures: list[str]
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_markdown(self) -> str:
        lines = [f"# Eval report ({self.tier})", "",
                 f"Result: **{'PASS' if self.ok else 'FAIL'}** · tokens {int(self.cost.get('prompt_tokens', 0))} in / "
                 f"{int(self.cost.get('completion_tokens', 0))} out · {self.cost.get('seconds', 0):.1f} s", ""]
        if self.failures:
            lines += ["## Failed gates", "", *[f"- {f}" for f in self.failures], ""]
        lines += ["## Metrics", "", "| Metric | Value | Gate |", "|--------|-------|------|"]
        for name in sorted(self.metrics):
            gate = {True: "pass", False: "FAIL"}.get(self.gates.get(name), "-")
            lines.append(f"| {name} | {self.metrics[name]:.3f} | {gate} |")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict:
        return asdict(self)


def load_thresholds(path: Path = THRESHOLDS) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: abs(a - b) < 1e-9}


def evaluate_gates(metrics: dict[str, float], gates: list[dict], tier: str) -> tuple[dict[str, bool], list[str]]:
    """Check every gate of this tier. A gated metric that wasn't measured fails."""
    passed, failures = {}, []
    for g in gates:
        if g["tier"] not in (tier, "both"):
            continue
        name, value = g["metric"], metrics.get(g["metric"])
        if value is None:
            passed[name] = False
            failures.append(f"{name}: missing")
            continue
        passed[name] = _OPS[g["op"]](value, g["value"])
        if not passed[name]:
            failures.append(f"{name}: {value:.3f} {g['op']} {g['value']} failed")
    return passed, failures


def regressions(current: dict[str, float], baseline: dict[str, float], tolerance: float = 0.05) -> list[str]:
    return [f"{m}: {current[m]:.3f} regressed from baseline {baseline[m]:.3f} (> {tolerance})"
            for m in HIGHER_IS_BETTER if m in current and m in baseline and current[m] < baseline[m] - tolerance]


def write_report(report: EvalReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_markdown(), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    return path


def load_jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (DATASETS / name).read_text(encoding="utf-8").splitlines() if line.strip()]


class JsonlSource:
    """Dataset rows as a review source (no network): noon UTC on the row's date."""

    name = "play_store"

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetch(self, since: date, until: date) -> list[RawReview]:
        return [RawReview(r["id"], r["rating"], None, r["text"], datetime.combine(date.fromisoformat(r["date"]),
                                                                                  dtime(12)), None)
                for r in self.rows if since <= date.fromisoformat(r["date"]) <= until]


# --- offline LLM: a keyword labeler and a template writer -----------------------------------------
KEYWORDS = {
    "App Performance & Bugs": ("crash", "freez", "slow", "hang", "blank", "load", "logs me out", "error",
                               "notification"),
    "Customer Support": ("support", "customer care", "ticket", "helpline", "complaint"),
    "Charges & Fees": ("charge", "brokerage", "fee"),
    "Withdrawals & Settlements": ("withdraw", "settlement", "credited"),
    "Payments & Fund Transfer": ("upi", "add money", "payment", "add funds", "transfer"),
    "Onboarding & KYC": ("kyc", "verification", "account opening", "document"),
    "Statements & Reports": ("report", "statement", "contract note"),
    "Trading/Order Execution": ("order", "stop loss", "executed", "scalper"),
    "Charts & Indicators": ("chart", "candle", "indicator", "drawing"),
    "Mutual Funds/SIP": ("sip", "mutual fund", "nav", "fund"),
    "IPO": ("ipo", "allotment"),
    "Feature Requests": ("please add", "please let"),
    "Ease of Use & Praise": ("easy", "simple", "smooth", "great", "clean", "beginners"),
}


def _guess_theme(text: str) -> str:
    text = text.lower()
    hits = {theme: sum(text.count(k) for k in keys) for theme, keys in KEYWORDS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] else "General Feedback"


def _respond(system: str, user: str, schema: dict, leak: bool = False) -> dict:
    if "<data>" in user:
        ctx = json.loads(user.split("<data>")[1].split("</data>")[0])
        names = [t["name"] for t in ctx["themes"]]
        summaries = [f"Users report repeated problems with {n.lower()} in recent weeks." for n in names]
        if leak:
            summaries[0] = "Users wrote to help@groww.in about this."
        return {"theme_summaries": summaries,
                "actions": [f"{names[i % len(names)]}: fix the top complaint and cut its weekly review volume by "
                            f"20% within 4 weeks." for i in range(3)]}
    blocks = re.findall(r'<cluster id="(\d+)">(.*?)</cluster>', user, re.S)
    key = "clusters" if "clusters" in schema["properties"] else "mapping"
    out = []
    for c, body in blocks:
        name = re.search(r"<name>(.*?)</name>", body)
        theme = name.group(1).replace("&amp;", "&") if name else _guess_theme(body)
        out.append({"cluster": int(c), "name": theme, "description": f"Reviews about {theme.lower()}."})
    return {key: out}


def offline_llm(leak: bool = False) -> FakeLLM:
    return FakeLLM([lambda s, u, schema: _respond(s, u, schema, leak)] * 500)


# --- one pipeline run ---------------------------------------------------------------------------------------
def _pipeline(settings: Settings, rows: list[dict], llm, embedder, work: Path, name: str) -> dict:
    s = settings.model_copy(update={"data_dir": work / name})
    before = dict(getattr(llm, "usage", {}))
    start = time.perf_counter()
    result = run_pipeline(s, EVAL_WEEK, dry_run=True, source=JsonlSource(rows), llm=llm, embedder=embedder,
                          today=EVAL_TODAY, sleep=lambda _: None)
    seconds = time.perf_counter() - start
    usage = {k: v - before.get(k, 0) for k, v in getattr(llm, "usage", {}).items()}
    out = run_dir(s, EVAL_WEEK)
    with Database(db_path(s)) as db:
        themes = db.load_themes(EVAL_WEEK)
        reviews = {r.id: r for r in db.reviews_in_window(*week_window(EVAL_WEEK, s.max_window_weeks))}
    quotes = ([Quote(**q) for q in json.loads((out / "quotes.json").read_text(encoding="utf-8"))["quotes"]]
              if (out / "quotes.json").exists() else [])
    meta = json.loads((out / "themes.json").read_text(encoding="utf-8")) if (out / "themes.json").exists() else {}
    run = json.loads((out / "run.json").read_text(encoding="utf-8")) if (out / "run.json").exists() else {}
    markdown = result.pulse_path.read_text(encoding="utf-8") if result.pulse_path else ""
    top = [t for tid in meta.get("top", []) for t in themes if t.id == tid]
    artifacts = " ".join(p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file())
    return {
        "status": result.status, "error": result.error, "markdown": markdown, "themes": themes, "top": top,
        "quotes": quotes, "reviews": reviews, "seconds": seconds, "usage": usage,
        "gold": {review_id(s.id_salt, r["id"]): r["gold_theme"] for r in rows},
        "fidelity": (sum(_quote_ok(q.text, reviews.get(q.review_id)) for q in quotes) / len(quotes)) if quotes else 0.0,
        "structure": float(bool(run.get("validation", {}).get("ok")) and len(top) == 3 and len(quotes) == 3
                           and bool(markdown)),
        "pii": len(find_pattern_pii(_HASH_OR_FLOAT.sub(" ", artifacts))),
    }


def _quality(run: dict, embedder) -> dict[str, float]:
    assign = {rid: t.id for t in run["themes"] for rid in t.review_ids}
    majority = gold_majority(run["themes"], run["gold"])
    scores = cluster_scores(assign, run["gold"])
    return {"cluster_purity": scores["purity"], "cluster_nmi": scores["nmi"],
            "theme_name_match": theme_name_match(run["themes"], majority, embedder),
            "quote_relevance": quote_relevance(run["quotes"], run["gold"], majority)}


def _judge_context(run: dict) -> dict:
    return {"themes": [{"name": t.name, "description": t.description, "share": round(t.share, 2),
                        "sample_snippets": [run["reviews"][r].text for r in t.review_ids[:5] if r in run["reviews"]]}
                       for t in run["top"]]}


# --- tiers ----------------------------------------------------------------------------------------------------
def _pii_metrics() -> dict[str, float]:
    rows = load_jsonl("pii_seeded.jsonl")
    scores = pii_scores([pii_spans(r["text"]) for r in rows], [r["spans"] for r in rows])
    out = {f"pii_recall_{t}": scores[t]["recall"] for t in PII_TYPES if t in scores}
    out["pii_precision_overall"] = scores["overall"]["precision"]
    return out


def _edge_case_pass_rate() -> float:
    proc = subprocess.run([sys.executable, "-m", "pytest", str(REPO / "tests" / "edge_cases"), "-q",
                           "-p", "no:cacheprovider"], cwd=REPO, capture_output=True, text=True)
    count = lambda word: sum(int(n) for n in re.findall(rf"(\d+) {word}", proc.stdout))  # noqa: E731
    passed, failed = count("passed"), count("failed") + count("error")
    return passed / (passed + failed) if passed + failed else 0.0


def _scenario(settings: Settings, rows: list[dict], work: Path, name: str, agent_llm) -> dict:
    """One agent scenario against a fresh data dir and fake MCP server; success is judged from outcomes."""
    s = settings.model_copy(update={"data_dir": work / name})
    server = make_fake_server()
    factory = fake_session_factory(server)

    def gateway(kind: str) -> McpGateway:
        return McpGateway(getattr(s.mcp, kind), kind, s.mcp.timeout_s, session_factory=factory)

    kind = name.split("-")[0]
    deps = dict(source=JsonlSource(rows), embedder=HashEmbedder(dim=256), today=EVAL_TODAY, sleep=lambda _: None)
    if kind in ("resume", "republish"):
        run_pipeline(s, EVAL_WEEK, dry_run=kind == "resume", llm=offline_llm(),
                     gateways=(gateway("docs"), gateway("gmail")), **deps)
    if kind == "flakydocs":
        server.fail_next["create_document"] = RuntimeError("docs down")
    ctx = RunContext(s, EVAL_WEEK, RunDeps(llm=offline_llm(leak=kind == "unvalidatable"), **deps))
    result = asyncio.run(run_agent(ctx, agent_llm, AgentTools(ctx, gateway)))
    docs, drafts = len(server.docs), len(server.drafts)
    expected = {
        "unvalidatable": result.status == "failed" and docs == drafts == 0,
        "flakydocs": drafts == 1 and docs <= 1 and result.status in ("ok", "partial"),
    }.get(kind, result.status == "ok" and docs == drafts == 1)
    violation = bool(result.reason in ("agent_step_limit", "agent_repeat_limit") or docs > 1 or drafts > 1
                     or (kind == "unvalidatable" and docs + drafts > 0))
    return {"scenario": name, "status": result.status, "reason": result.reason, "steps": result.steps,
            "success": expected, "violation": violation, "docs": docs, "drafts": drafts}


def run_evals(
    settings: Settings,
    tier: Literal["offline", "live"],
    repeats: int = 3,
    *,
    work_dir: Path | None = None,
    edge_cases: bool = True,
    llm_factory: Callable[[], object] | None = None,
    judge_llm=None,
    embedder=None,
    agent_llm_factory: Callable[[], object] | None = None,
    baseline: Path | None = BASELINE,
) -> EvalReport:
    import tempfile

    work = Path(work_dir or tempfile.mkdtemp(prefix="pulse-eval-"))
    started = time.perf_counter()
    labeled = load_jsonl("reviews_labeled.jsonl")
    metrics: dict[str, float] = {}
    details: dict = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    if tier == "offline":
        metrics |= _pii_metrics()
        embedder = embedder or HashEmbedder(dim=256)
        run = _pipeline(settings, labeled, (llm_factory or offline_llm)(), embedder, work, "offline")
        details["runs"] = [{"status": run["status"], "error": run["error"]}]
        metrics |= {"quote_fidelity": run["fidelity"], "structure": run["structure"], "pii_in_artifacts": run["pii"]}
        metrics |= _quality(run, embedder)  # informational offline: the fake labeler isn't a model
        if edge_cases:
            metrics["edge_case_pass_rate"] = _edge_case_pass_rate()
    else:
        if embedder is None:
            from groww_pulse.themes.embed import Embedder

            embedder = Embedder()
        llm_factory = llm_factory or (lambda: GroqClient(settings.model, settings.fallback_model,
                                                         settings.reasoning_effort, settings.temperature))
        judge = judge_llm or judge_client(settings)
        rows = labeled + load_jsonl("injection.jsonl")
        runs, per_run = [], []
        for i in range(repeats):
            run = _pipeline(settings, rows, llm_factory(), embedder, work, f"live{i}")
            for k in usage:
                usage[k] += run["usage"].get(k, 0)
            m = {"quote_fidelity": run["fidelity"], "structure": run["structure"], "pii_in_artifacts": run["pii"],
                 "tokens": sum(run["usage"].values()), "seconds": run["seconds"], **_quality(run, embedder)}
            if run["markdown"]:
                try:
                    score = judge_pulse(run["markdown"], _judge_context(run), judge)
                    m |= {"judge_grounded": score.grounded, "judge_specific_actions": score.specific_actions,
                          "judge_clarity": score.clarity, "judge_followed_injection": int(score.followed_injection)}
                except LLMContractError as e:
                    m["judge_error"] = 1
                    details.setdefault("judge_errors", []).append(str(e)[:200])
                actions = [line.split(". ", 1)[1] for line in run["markdown"].split("## Action Ideas")[1].splitlines()
                           if re.match(r"^\d\. ", line)]
                m["action_specificity"] = sum(action_specificity_heuristic(a, [t.name for t in run["top"]])
                                              for a in actions)
            runs.append(run)
            per_run.append(m)
            details.setdefault("runs", []).append({"status": run["status"], "error": run["error"],
                                                   "themes": [t.name for t in run["themes"]]})

        def mean(key: str) -> float:
            values = [m[key] for m in per_run if key in m]
            return statistics.fmean(values) if values else float("nan")

        metrics |= {"quote_fidelity": min(m["quote_fidelity"] for m in per_run),
                    "structure": min(m["structure"] for m in per_run),
                    "pii_in_artifacts": max(m["pii_in_artifacts"] for m in per_run),
                    "tokens_per_run_max": max(m["tokens"] for m in per_run),
                    "seconds_per_run_max": max(m["seconds"] for m in per_run)}
        for key in ("cluster_purity", "cluster_nmi", "theme_name_match", "quote_relevance", "judge_grounded",
                    "judge_specific_actions", "judge_clarity"):
            metrics[key] = mean(key)
            metrics[f"{key}_min"] = min((m[key] for m in per_run if key in m), default=float("nan"))
        metrics["judge_followed_injection"] = sum(m.get("judge_followed_injection", 0) for m in per_run)
        metrics["action_specificity_min"] = min((m.get("action_specificity", 0) for m in per_run), default=0)
        pairs = list(zip(runs, runs[1:]))
        metrics["name_stability"] = (statistics.fmean(name_stability([t.name for t in a["themes"]],
                                                                     [t.name for t in b["themes"]]) for a, b in pairs)
                                     if pairs else float("nan"))
        agent_factory = agent_llm_factory or (lambda: GroqClient(settings.model, settings.fallback_model,
                                                                 settings.agent.reasoning_effort, settings.temperature))
        scenarios = [_scenario(settings, labeled, work, f"{kind}-{i}", agent_factory())
                     for i in range(repeats)
                     for kind in ("fresh", "resume", "unvalidatable", "flakydocs", "republish")]
        details["agent"] = scenarios
        metrics["agent_success"] = sum(s["success"] for s in scenarios) / len(scenarios)
        metrics["agent_guardrail_violations"] = sum(s["violation"] for s in scenarios)

    thresholds = load_thresholds()
    gate_list = [g for g in thresholds["gates"] if edge_cases or g["metric"] != "edge_case_pass_rate"]
    gates, failures = evaluate_gates(metrics, gate_list, tier)
    if tier == "live" and baseline and Path(baseline).exists():
        base = json.loads(Path(baseline).read_text(encoding="utf-8"))["metrics"]
        regressed = regressions(metrics, base, thresholds.get("regression_tolerance", 0.05))
        gates["no_regression"] = not regressed
        failures += regressed
    cost = {**usage, "seconds": round(time.perf_counter() - started, 1)}
    return EvalReport(tier, metrics, gates, cost, failures, details)
