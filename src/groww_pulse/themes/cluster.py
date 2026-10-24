"""KMeans clustering with k chosen by silhouette score."""

import warnings

import numpy as np
from sklearn.cluster import KMeans
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import silhouette_score

SILHOUETTE_SAMPLE = 3000  # silhouette on a sample keeps large inputs fast


def cluster(emb: np.ndarray, k_min: int = 3, k_max: int = 8, seed: int = 42) -> np.ndarray:
    """Integer labels of length n. Too few points, or no k that splits them, gives a single cluster.

    Below 2 * k_min points there aren't two reviews per cluster, so everything stays in one cluster.
    """
    n = len(emb)
    best_labels, best_score = np.zeros(n, dtype=int), -1.0
    if n < 2 * k_min:
        return best_labels
    ks = range(k_min, min(k_max, n - 1) + 1)
    for k in ks:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)  # duplicate points: fewer distinct clusters
            labels = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(emb)
        if len(set(labels)) < 2:
            continue
        if len(ks) == 1:  # a fixed k needs no score; silhouette is O(n^2) and 20k reviews would take minutes
            return labels.astype(int)
        s = silhouette_score(emb, labels, sample_size=min(n, SILHOUETTE_SAMPLE), random_state=seed)
        if s > best_score:
            best_labels, best_score = labels.astype(int), s
    return best_labels
