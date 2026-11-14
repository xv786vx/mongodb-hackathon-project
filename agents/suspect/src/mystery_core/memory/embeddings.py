"""Embedding clients. Set EMBEDDING_MODEL=fake to use FakeEmbedder everywhere (tests, offline dev).

Whatever embedded the stored memories must also embed the queries: don't mix fake and Voyage in one database.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from .config import Settings


class Embedder(Protocol):
    dims: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class VoyageEmbedder:
    BATCH = 128

    def __init__(self, api_key: str, model: str = "voyage-4-lite", dims: int = 1024):
        import voyageai  # imported lazily so FakeEmbedder users don't need the key

        self._client = voyageai.AsyncClient(api_key=api_key)
        self.model = model
        self.dims = dims

    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            res = await self._client.embed(
                texts[i : i + self.BATCH],
                model=self.model,
                input_type=input_type,
                output_dimension=self.dims,
            )
            out.extend(res.embeddings)
        for v in out:
            if len(v) != self.dims:
                raise RuntimeError(f"embedding has {len(v)} dims, expected {self.dims} (EMBEDDING_DIMS)")
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "document") if texts else []

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]


_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Deterministic bag-of-words hashing. Texts sharing words are similar; no API, no network."""

    def __init__(self, dims: int = 1024):
        self.dims = dims

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for word in _WORD.findall(text.lower()):
            h = int.from_bytes(hashlib.sha1(word.encode()).digest()[:8], "big")
            v[h % self.dims] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            v[0], norm = 1.0, 1.0
        return [x / norm for x in v]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def embedder_from_settings(settings: Settings) -> Embedder:
    if settings.embedding_model == "fake":
        return FakeEmbedder(settings.embedding_dims)
    if not settings.embedding_api_key:
        raise RuntimeError("EMBEDDING_API_KEY is not set (or set EMBEDDING_MODEL=fake)")
    return VoyageEmbedder(settings.embedding_api_key, settings.embedding_model, settings.embedding_dims)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
