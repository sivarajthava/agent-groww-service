import numpy as np

from groww_pulse.themes.cluster import cluster


def _blobs(k=3, per=20, dim=16, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(k, dim)) * 5
    x = np.vstack([c + rng.normal(scale=0.3, size=(per, dim)) for c in centers])
    truth = np.repeat(np.arange(k), per)
    return x.astype(np.float32), truth


def test_three_blobs_give_three_clusters():
    x, truth = _blobs()
    labels = cluster(x)
    assert len(set(labels)) == 3
    # every true blob maps to exactly one label
    assert all(len(set(labels[truth == t])) == 1 for t in range(3))


def test_five_blobs_give_five_clusters():
    x, _ = _blobs(k=5, per=15)
    assert len(set(cluster(x))) == 5


def test_deterministic_with_seed():
    x, _ = _blobs(k=4, per=10, seed=3)
    assert np.array_equal(cluster(x, seed=7), cluster(x, seed=7))


def test_labels_are_ints_of_length_n():
    x, _ = _blobs()
    labels = cluster(x)
    assert labels.shape == (60,) and np.issubdtype(labels.dtype, np.integer)


def test_tiny_input_single_cluster():
    x, _ = _blobs(k=2, per=2)  # 4 vectors < k_min + 1
    labels = cluster(x)
    assert labels.tolist() == [0, 0, 0, 0]


def test_empty_and_identical_inputs_do_not_crash():
    assert cluster(np.zeros((0, 8), dtype=np.float32)).shape == (0,)
    same = np.ones((10, 8), dtype=np.float32)  # silhouette is undefined for identical points
    assert set(cluster(same).tolist()) == {0}
