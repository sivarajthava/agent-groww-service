"""Theme extraction: embed → cluster → LLM label/merge (≤ max_themes) → assign every review to a theme."""

import re

import numpy as np

from groww_pulse.llm import LLMClient
from groww_pulse.models import Review, Theme
from groww_pulse.normalize.normalize import is_noise
from groww_pulse.themes.cluster import cluster
from groww_pulse.themes.label import label_and_merge

OTHER = "other"  # internal bucket for reviews close to no theme; never shown in the pulse
MIN_CLUSTER_SIZE = 3  # smaller clusters are outliers, not themes; their reviews are assigned like any other


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return (v / n if n else v).astype(np.float32)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "theme"


def extract_themes(
    reviews: list[Review],
    embedder,
    llm: LLMClient,
    previous_names: list[str],
    max_themes: int = 5,
    samples_per_cluster: int = 15,
    other_threshold: float = 0.2,
) -> tuple[list[Theme], dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Returns (themes by count desc, centroid by theme_id, embedding by review_id).

    Only English, non-noise reviews are clustered. `share` is relative to all `reviews`.
    """
    eligible = [r for r in reviews if r.lang == "en" and not is_noise(r.text)]
    if not eligible:
        return [], {}, {}
    x = embedder.encode([r.text for r in eligible])
    labels = cluster(x)

    found = sorted(set(labels.tolist()))
    big = [c for c in found if (labels == c).sum() >= MIN_CLUSTER_SIZE]
    big = big or [max(found, key=lambda c: (labels == c).sum())]
    centroids, sizes, samples = {}, {}, {}
    for c in big:
        idx = np.flatnonzero(labels == c)
        centroids[c] = _unit(x[idx].mean(axis=0))
        sizes[c] = len(idx)
        typical = idx[np.argsort(-(x[idx] @ centroids[c]), kind="stable")][:samples_per_cluster]
        samples[c] = [eligible[i].text for i in typical]
    names = label_and_merge(samples, previous_names, llm, max_themes, centroids=centroids, sizes=sizes)

    # One centroid per final theme, from all reviews of its clusters.
    theme_of_cluster = {c: names[c] for c in centroids}
    theme_keys = sorted(set(theme_of_cluster.values()))
    theme_centroid = np.stack(
        [_unit(x[np.isin(labels, [c for c, k in theme_of_cluster.items() if k == key])].mean(axis=0))
         for key in theme_keys]
    )

    sims = x @ theme_centroid.T
    best = sims.argmax(axis=1)
    members: dict[int, list[Review]] = {}
    for i, r in enumerate(eligible):
        if sims[i, best[i]] >= other_threshold:
            members.setdefault(int(best[i]), []).append(r)

    themes, out_centroids, used_ids = [], {}, set()
    for k, (name, description) in enumerate(theme_keys):
        rs = members.get(k, [])
        if not rs:
            continue
        tid = _slug(name)
        while tid in used_ids or tid == OTHER:
            tid += "-2"
        used_ids.add(tid)
        ratings = [r.rating for r in rs]
        themes.append(
            Theme(
                id=tid,
                name=name,
                description=description,
                review_ids=tuple(r.id for r in rs),
                count=len(rs),
                share=len(rs) / len(reviews),
                avg_rating=sum(ratings) / len(ratings),
                neg_share=sum(x <= 2 for x in ratings) / len(ratings),
                wow_delta=None,
            )
        )
        out_centroids[tid] = theme_centroid[k]
    themes.sort(key=lambda t: (-t.count, t.name))
    return themes, out_centroids, {r.id: x[i] for i, r in enumerate(eligible)}
