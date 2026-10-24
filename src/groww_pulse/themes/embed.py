"""Sentence embeddings. The real model runs locally on CPU, so review text never leaves the machine."""

import hashlib
import re

import numpy as np


def _unit_rows(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return (x / np.where(norms == 0, 1, norms)).astype(np.float32)


class Embedder:
    """all-MiniLM-L6-v2 via sentence-transformers: 384-d, L2-normalised, float32. Loaded on first use."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import, deferred

            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        model = self._load()
        if not texts:
            return np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)
        emb = model.encode(texts, batch_size=64, convert_to_numpy=True, normalize_embeddings=True)
        return emb.astype(np.float32)


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder for tests: no model download, L2-normalised."""

    def __init__(self, dim: int = 64):
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"\w+", text.lower()):
                out[i, int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1
        return _unit_rows(out)
