"""LLM naming of clusters, merged down to at most `max_themes` distinct themes."""

import html
import json

import numpy as np

from groww_pulse.llm import LLMClient, LLMContractError
from groww_pulse.prompts import load_prompt


def _entries_schema(key: str) -> dict:
    entry = {
        "type": "object",
        "properties": {"cluster": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}},
        "required": ["cluster", "name", "description"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {key: {"type": "array", "items": entry}},
        "required": [key],
        "additionalProperties": False,
    }


LABEL_SCHEMA = _entries_schema("clusters")
MERGE_SCHEMA = _entries_schema("mapping")


def _previous(previous_names: list[str]) -> str:
    return f"Previous week's theme names: {json.dumps(previous_names) if previous_names else 'none'}"


def _label_prompt(samples: dict[int, list[str]], previous_names: list[str]) -> str:
    blocks = []
    for c, texts in sorted(samples.items()):
        reviews = "\n".join(f"<review>{html.escape(t, quote=False)}</review>" for t in texts)
        blocks.append(f'<cluster id="{c}">\n{reviews}\n</cluster>')
    return f"{_previous(previous_names)}\n\n<clusters>\n" + "\n".join(blocks) + "\n</clusters>"


def _merge_prompt(labels: dict[int, tuple[str, str]], sizes: dict[int, int], previous_names, max_themes) -> str:
    blocks = [
        f'<cluster id="{c}">\n<name>{html.escape(n)}</name>\n<description>{html.escape(d)}</description>\n'
        f"<size>{sizes[c]}</size>\n</cluster>"
        for c, (n, d) in sorted(labels.items())
    ]
    return (
        f"Maximum themes: {max_themes}\n{_previous(previous_names)}\n\n<clusters>\n"
        + "\n".join(blocks)
        + "\n</clusters>"
    )


def _parse(entries: list[dict], clusters: set[int]) -> dict[int, tuple[str, str]]:
    out = {}
    for e in entries:
        name = e["name"].strip()
        if e["cluster"] in clusters and name:
            out[e["cluster"]] = (name, e["description"].strip())
    return out


def _fold_smallest(
    labels: dict[int, tuple[str, str]],
    max_themes: int,
    sizes: dict[int, int],
    centroids: dict[int, np.ndarray] | None,
) -> dict[int, tuple[str, str]]:
    """Fold the smallest theme into its nearest theme (by centroid) until at most max_themes remain."""
    labels = dict(labels)
    while len(set(labels.values())) > max_themes:
        groups: dict[tuple[str, str], list[int]] = {}
        for c, lab in labels.items():
            groups.setdefault(lab, []).append(c)
        size = {lab: sum(sizes[c] for c in cs) for lab, cs in groups.items()}
        smallest = min(groups, key=lambda lab: (size[lab], lab))
        others = [lab for lab in groups if lab != smallest]
        if centroids is not None:
            centre = {lab: np.mean([centroids[c] for c in cs], axis=0) for lab, cs in groups.items()}

            def cos(a, b):
                return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) or 1))

            target = max(others, key=lambda lab: cos(centre[smallest], centre[lab]))
        else:
            target = max(others, key=lambda lab: size[lab])
        for c in groups[smallest]:
            labels[c] = target
    return labels


def label_and_merge(
    samples: dict[int, list[str]],
    previous_names: list[str],
    llm: LLMClient,
    max_themes: int = 5,
    centroids: dict[int, np.ndarray] | None = None,
    sizes: dict[int, int] | None = None,
) -> dict[int, tuple[str, str]]:
    """Map each cluster label to (theme_name, description), with at most max_themes distinct names."""
    clusters = set(samples)
    sizes = sizes or {c: len(texts) for c, texts in samples.items()}
    resp = llm.complete_json(load_prompt("label_themes"), _label_prompt(samples, previous_names), LABEL_SCHEMA)
    labels = _parse(resp["clusters"], clusters)
    missing = clusters - labels.keys()
    if missing:
        raise LLMContractError(f"label_themes: no label for cluster(s) {sorted(missing)}")

    if len({n for n, _ in labels.values()}) > max_themes:
        resp = llm.complete_json(
            load_prompt("merge_themes"),
            _merge_prompt(labels, sizes, previous_names, max_themes),
            MERGE_SCHEMA,
            reasoning_effort="high",
        )
        labels |= _parse(resp["mapping"], clusters)  # a cluster the merge skipped keeps its first-pass label

    # Same name means same theme: give every cluster of a name one shared description.
    by_name = {}
    for c in sorted(labels):
        by_name.setdefault(labels[c][0], labels[c])
    labels = {c: by_name[name] for c, (name, _) in labels.items()}
    return _fold_smallest(labels, max_themes, sizes, centroids)
