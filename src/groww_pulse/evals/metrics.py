"""Eval metrics (implementation plan, Task 8.2). Pure functions over spans, assignments, themes and quotes."""

import re
from collections import Counter

import numpy as np
from sklearn.metrics import normalized_mutual_info_score

from groww_pulse.models import Quote, Theme
from groww_pulse.themes.taxonomy import OTHER, TAXONOMY

MIN_OVERLAP = 0.5  # a predicted span matches a gold span when it covers at least half of it


def _covers(pred: dict, gold: dict) -> bool:
    overlap = min(pred["end"], gold["end"]) - max(pred["start"], gold["start"])
    return overlap >= MIN_OVERLAP * (gold["end"] - gold["start"])


def _ratio(hits: int, total: int) -> float:
    return hits / total if total else 1.0  # nothing to find / nothing predicted counts as perfect


def pii_scores(pred_spans: list[list[dict]], gold_spans: list[list[dict]]) -> dict[str, dict[str, float]]:
    """Per-type and overall precision/recall over rows of spans ({start, end, type}).

    Recall of a gold type: share of its spans covered by any prediction. Precision of a predicted type: share
    of its predictions that cover some gold span (of any type, since every PII type is redacted alike).
    """
    gold_hit, gold_n, pred_hit, pred_n = Counter(), Counter(), Counter(), Counter()
    for preds, golds in zip(pred_spans, gold_spans, strict=True):
        for g in golds:
            gold_n[g["type"]] += 1
            gold_hit[g["type"]] += any(_covers(p, g) for p in preds)
        for p in preds:
            pred_n[p.get("type")] += 1
            pred_hit[p.get("type")] += any(_covers(p, g) for g in golds)
    out = {t: {"precision": _ratio(pred_hit[t], pred_n[t]), "recall": _ratio(gold_hit[t], gold_n[t])}
           for t in sorted(set(gold_n) | {t for t in pred_n if t})}
    out["overall"] = {"precision": _ratio(sum(pred_hit.values()), sum(pred_n.values())),
                      "recall": _ratio(sum(gold_hit.values()), sum(gold_n.values()))}
    return out


def cluster_scores(assign: dict[str, str], gold: dict[str, str]) -> dict[str, float]:
    """Purity and NMI of a review -> theme assignment against gold labels (ids without a gold label are skipped)."""
    ids = [i for i in assign if i in gold]
    if not ids:
        return {"purity": 0.0, "nmi": 0.0}
    by_cluster: dict[str, Counter] = {}
    for i in ids:
        by_cluster.setdefault(assign[i], Counter())[gold[i]] += 1
    purity = sum(c.most_common(1)[0][1] for c in by_cluster.values()) / len(ids)
    nmi = normalized_mutual_info_score([gold[i] for i in ids], [str(assign[i]) for i in ids])
    return {"purity": purity, "nmi": float(nmi)}


def gold_majority(themes: list[Theme], gold: dict[str, str]) -> dict[str, str]:
    """The most common gold label among each theme's reviews."""
    out = {}
    for t in themes:
        labels = Counter(gold[r] for r in t.review_ids if r in gold)
        if labels:
            out[t.id] = labels.most_common(1)[0][0]
    return out


def theme_name_match(themes: list[Theme], gold_majority: dict[str, str], embedder) -> float:
    """Share of themes whose name is closest (cosine) to the taxonomy label that is their gold majority."""
    graded = [t for t in themes if t.id in gold_majority]
    if not graded:
        return 0.0
    labels = [*TAXONOMY, OTHER]
    label_vecs = embedder.encode(labels)
    name_vecs = embedder.encode([t.name for t in graded])
    nearest = np.argmax(name_vecs @ label_vecs.T, axis=1)
    return sum(labels[n] == gold_majority[t.id] for t, n in zip(graded, nearest)) / len(graded)


def quote_relevance(quotes: list[Quote], gold: dict[str, str], gold_majority: dict[str, str]) -> float:
    """Share of quotes whose own gold label equals the gold majority of the theme they were picked for."""
    if not quotes:
        return 0.0
    return sum(gold.get(q.review_id) is not None and gold.get(q.review_id) == gold_majority.get(q.theme_id)
               for q in quotes) / len(quotes)


def name_stability(run_a: list[str], run_b: list[str]) -> float:
    """Jaccard similarity of two runs' theme names."""
    a, b = set(run_a), set(run_b)
    return len(a & b) / len(a | b) if a | b else 1.0


_MEASURABLE = re.compile(
    r"\d|%|\b(?:daily|weekly|monthly|quarter|sprint|within|by (?:end|next|q\d)|sla|nps|csat|rate|latency|"
    r"p9\d|count|volume|tickets?|crash-free|time to|response time|target|kpi)\b",
    re.IGNORECASE,
)


def action_specificity_heuristic(action: str, theme_names: list[str] | None = None) -> bool:
    """True when the action names a theme and has something measurable (a number, %, a time bound, a metric)."""
    if theme_names is not None:
        names_theme = any(n.lower() in action.lower() for n in theme_names)
    else:
        names_theme = bool(re.match(r"^[^:\n]{2,60}:\s", action))  # "Theme Name: step ..." per the prompt contract
    return names_theme and bool(_MEASURABLE.search(action))
