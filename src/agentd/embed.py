"""Embeddings and reranking on CPU (ONNX, no torch), plus a deterministic test double."""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Protocol

from .config import Config, get_config


class Embedder(Protocol):
    model_name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """bge-small via fastembed. Model files download once, then load from disk."""

    def __init__(self, cfg: Config | None = None) -> None:
        cfg = cfg or get_config()
        self.model_name = cfg.embed.model
        self.dim = cfg.embed.dim
        self._model = None
        self._reranker = None
        self._rerank_name = cfg.embed.rerank_model
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    self._model = await asyncio.to_thread(TextEmbedding, model_name=self.model_name)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = await self._ensure()
        vectors = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [v.tolist() for v in vectors]

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Cross-encoder scores. Falls back to zeros when the model is unavailable."""
        if not documents:
            return []
        try:
            if self._reranker is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                self._reranker = await asyncio.to_thread(
                    TextCrossEncoder, model_name=self._rerank_name
                )
            scores = await asyncio.to_thread(
                lambda: list(self._reranker.rerank(query, documents))
            )
            return [float(s) for s in scores]
        except Exception:
            return [0.0] * len(documents)


class HashEmbedder:
    """Deterministic bag-of-words embedding: no downloads, cosine still meaningful."""

    model_name = "hash-test"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int(hashlib.sha1(token.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        qv = self._one(query)
        return [sum(a * b for a, b in zip(qv, self._one(d), strict=True)) for d in documents]


_embedder: Embedder | None = None
_disabled = False


def get_embedder(cfg: Config | None = None) -> Embedder | None:
    """None means 'run without vectors' — keyword search and recency still work."""
    global _embedder
    if _disabled:
        return None
    if _embedder is None:
        cfg = cfg or get_config()
        if not cfg.embed.enabled:
            return None
        _embedder = FastEmbedder(cfg)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder, _disabled
    _embedder = embedder
    _disabled = embedder is None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
