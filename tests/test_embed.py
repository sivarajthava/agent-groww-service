import numpy as np
import pytest

from groww_pulse.themes.embed import Embedder, HashEmbedder


def test_hash_embedder_shape_and_norm():
    emb = HashEmbedder(dim=64).encode(["withdrawal stuck for days", "app crashes on login", "charts are slow"])
    assert emb.shape == (3, 64)
    assert emb.dtype == np.float32
    assert np.allclose(np.linalg.norm(emb, axis=1), 1.0)


def test_hash_embedder_is_deterministic():
    a = HashEmbedder().encode(["withdrawal stuck for days"])
    b = HashEmbedder().encode(["Withdrawal stuck for days!"])
    assert np.array_equal(a, b)


def test_hash_embedder_empty_inputs():
    assert HashEmbedder(dim=16).encode([]).shape == (0, 16)
    assert not np.isnan(HashEmbedder().encode([""])).any()


def test_similar_texts_closer_than_unrelated():
    e = HashEmbedder(dim=256).encode(
        ["withdrawal money stuck for days", "money withdrawal stuck again", "chart indicators look great"]
    )
    assert e[0] @ e[1] > e[0] @ e[2]


@pytest.mark.slow
def test_real_embedder_shape_norm_and_semantics():
    e = Embedder().encode(
        ["My withdrawal is stuck for five days", "Money not credited to bank after withdrawal", "Love the new charts"]
    )
    assert e.shape == (3, 384) and e.dtype == np.float32
    assert np.allclose(np.linalg.norm(e, axis=1), 1.0, atol=1e-5)
    assert e[0] @ e[1] > e[0] @ e[2]


@pytest.mark.slow
def test_real_embedder_empty_input():
    assert Embedder().encode([]).shape == (0, 384)
