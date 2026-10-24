"""LLM naming of clusters, merged down to at most `max_themes` distinct themes."""

import html
import json

import numpy as np

from groww_pulse.guards import IMPERATIVE, MAX_NAME_WORDS, has_url
from groww_pulse.llm import LLMClient, LLMContractError, RequestTooLarge
from groww_pulse.prompts import load_prompt

RETRIES = 2  # contract failures (bad JSON, missing clusters, injected labels) are retried this many times


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
    """Each review is escaped and wrapped as <review id="rN"> inside a <reviews> block, so review text can't
    close its block or pose as instructions."""
    blocks, n = [], 0
    for c, texts in sorted(samples.items()):
        reviews = []
        for t in texts:
            n += 1
            reviews.append(f'<review id="r{n}">{html.escape(t, quote=False)}</review>')
        blocks.append(f'<cluster id="{c}">\n<reviews>\n' + "\n".join(reviews) + "\n</reviews>\n</cluster>")
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


def check_theme_label(name: str, description: str) -> None:
    """Raise LLMContractError for a label that echoes review instructions: a URL, a command, or not a short name."""
    if has_url(name) or has_url(description):
        raise LLMContractError(f"theme label contains a URL: {name!r}")
    words = name.split()
    if not 1 <= len(words) <= MAX_NAME_WORDS:
        raise LLMContractError(f"theme name must be 1-{MAX_NAME_WORDS} words: {name!r}")
    if IMPERATIVE.search(name):
        raise LLMContractError(f"theme name reads as an instruction: {name!r}")


def _parse(entries: list[dict], clusters: set[int]) -> dict[int, tuple[str, str]]:
    out = {}
    for e in entries:
        name = e["name"].strip()
        if e["cluster"] in clusters and name:
            check_theme_label(name, e["description"])
            out[e["cluster"]] = (name, e["description"].strip())
    return out


def _call(llm: LLMClient, prompt: str, user: str, schema: dict, key: str, clusters: set[int],
          require_all: bool, **kw) -> dict[int, tuple[str, str]]:
    """One labeling call with up to RETRIES retries on any contract failure."""
    for attempt in range(RETRIES + 1):
        try:
            labels = _parse(llm.complete_json(load_prompt(prompt), user, schema, **kw)[key], clusters)
            missing = clusters - labels.keys()
            if require_all and missing:
                raise LLMContractError(f"{prompt}: no label for cluster(s) {sorted(missing)}")
            return labels
        except RequestTooLarge:
            raise  # the same prompt would fail again; the caller sends fewer samples
        except LLMContractError:
            if attempt == RETRIES:
                raise
    raise AssertionError("unreachable")


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
    labels = _call(llm, "label_themes", _label_prompt(samples, previous_names), LABEL_SCHEMA, "clusters", clusters,
                   require_all=True)

    if len({n for n, _ in labels.values()}) > max_themes:
        try:
            merged = _call(llm, "merge_themes", _merge_prompt(labels, sizes, previous_names, max_themes), MERGE_SCHEMA,
                           "mapping", clusters, require_all=False, reasoning_effort="high")
        except LLMContractError:
            merged = {}  # keep the first-pass labels; the fold below still caps the count
        labels |= merged  # a cluster the merge skipped keeps its first-pass label

    # Same name means same theme: give every cluster of a name one shared description.
    by_name = {}
    for c in sorted(labels):
        by_name.setdefault(labels[c][0], labels[c])
    labels = {c: by_name[name] for c, (name, _) in labels.items()}
    return _fold_smallest(labels, max_themes, sizes, centroids)
