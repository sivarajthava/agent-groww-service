"""KMeans clustering with k chosen by silhouette score."""

import warnings

import numpy as np
from sklearn.cluster import KMeans
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import silhouette_score


def cluster(emb: np.ndarray, k_min: int = 3, k_max: int = 8, seed: int = 42) -> np.ndarray:
    """Integer labels of length n. Too few points, or no k that splits them, gives a single cluster.

    Below 2 * k_min points there aren't two reviews per cluster, so everything stays in one cluster.
    """
    n = len(emb)
    best_labels, best_score = np.zeros(n, dtype=int), -1.0
    if n < 2 * k_min:
        return best_labels
    for k in range(k_min, min(k_max, n - 1) + 1):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)  # duplicate points: fewer distinct clusters
            labels = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(emb)
        if len(set(labels)) < 2:
            continue
        s = silhouette_score(emb, labels)
        if s > best_score:
            best_labels, best_score = labels.astype(int), s
    return best_labels
