from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

from .ids import stable_vector


def _normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector: tuple[float, ...]
    provider: str
    model: str
    revision: str
    dimensions: int
    normalized: bool
    semantic: bool
    degraded: bool
    latency_ms: float
    fallback_model: str | None = None


def embed_with_metadata(provider: EmbeddingProvider, text: str) -> EmbeddingResult:
    method = getattr(provider, "embed_result", None)
    if callable(method):
        result = method(text)
        if not isinstance(result, EmbeddingResult):
            raise TypeError("embed_result must return EmbeddingResult")
        if len(result.vector) != provider.dimensions:
            raise ValueError(
                f"embedding dimensions do not match provider: expected {provider.dimensions}, "
                f"got {len(result.vector)}"
            )
        return result
    started = perf_counter()
    vector = provider.embed(text)
    if len(vector) != provider.dimensions:
        raise ValueError(
            f"embedding dimensions do not match provider: expected {provider.dimensions}, got {len(vector)}"
        )
    normalized = tuple(_normalise([float(value) for value in vector]))
    return EmbeddingResult(
        normalized,
        str(getattr(provider, "provider_name", type(provider).__name__)),
        provider.model,
        str(getattr(provider, "revision", "unversioned")),
        provider.dimensions,
        True,
        bool(getattr(provider, "semantic_similarity", True)),
        False,
        (perf_counter() - started) * 1000,
    )


@dataclass(frozen=True, slots=True)
class DeterministicEmbeddingProvider:
    """Offline embedding used by the local MVP and deterministic tests."""

    dimensions: int = 96
    model: str = "deterministic-sha256-v1"
    semantic_similarity: bool = False
    revision: str = "v1"
    provider_name: str = "deterministic-test"

    def embed(self, text: str) -> list[float]:
        return stable_vector(text, dimensions=self.dimensions)

    def embed_result(self, text: str) -> EmbeddingResult:
        started = perf_counter()
        vector = tuple(self.embed(text))
        return EmbeddingResult(vector, self.provider_name, self.model, self.revision, self.dimensions,
                               True, False, False, (perf_counter() - started) * 1000)


@dataclass(slots=True)
class OpenAICompatibleEmbeddingProvider:
    """Embedding adapter for OpenAI-compatible ``/embeddings`` endpoints."""

    endpoint: str
    model: str
    api_key: str
    dimensions: int
    timeout_seconds: float = 30.0
    fallback: EmbeddingProvider | None = None
    semantic_similarity: bool = True
    revision: str = "provider-managed"
    provider_name: str = "openai-compatible"

    def embed(self, text: str) -> list[float]:
        return list(self.embed_result(text).vector)

    def embed_result(self, text: str) -> EmbeddingResult:
        started = perf_counter()
        body = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            values = payload["data"][0]["embedding"]
            vector = [float(value) for value in values]
            if len(vector) != self.dimensions:
                raise ValueError(f"expected {self.dimensions} dimensions, got {len(vector)}")
            normalized = tuple(_normalise(vector))
            return EmbeddingResult(
                normalized, self.provider_name, self.model, self.revision, self.dimensions,
                True, True, False, (perf_counter() - started) * 1000,
            )
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            if self.fallback is None:
                raise RuntimeError("embedding provider request failed") from None
            fallback = embed_with_metadata(self.fallback, text)
            if len(fallback.vector) != self.dimensions:
                raise RuntimeError(
                    f"embedding fallback dimensions do not match provider: expected {self.dimensions}, "
                    f"got {len(fallback.vector)}"
                )
            return EmbeddingResult(
                tuple(_normalise(list(fallback.vector))), self.provider_name, self.model, self.revision,
                self.dimensions, True, False, True, (perf_counter() - started) * 1000,
                fallback_model=fallback.model,
            )


@dataclass(slots=True)
class LocalSentenceTransformerEmbeddingProvider:
    """Lazy local adapter; the core package does not depend on model runtimes."""

    encoder: Any
    model: str = "BAAI/bge-m3"
    revision: str = "unpinned"
    dimensions: int = 1024
    provider_name: str = "sentence-transformers"
    semantic_similarity: bool = True

    @classmethod
    def load(cls, *, model: str = "BAAI/bge-m3", revision: str,
             dimensions: int = 1024, device: str | None = None) -> "LocalSentenceTransformerEmbeddingProvider":
        if not revision or revision == "unpinned":
            raise ValueError("a pinned model revision is required")
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("install sentence-transformers in the optional model environment") from exc
        encoder = SentenceTransformer(model, revision=revision, device=device)
        return cls(encoder, model=model, revision=revision, dimensions=dimensions)

    def embed_result(self, text: str) -> EmbeddingResult:
        started = perf_counter()
        values = self.encoder.encode([text], normalize_embeddings=False)
        vector = [float(value) for value in values[0]]
        if len(vector) != self.dimensions:
            raise ValueError(f"expected {self.dimensions} dimensions, got {len(vector)}")
        normalized = tuple(_normalise(vector))
        return EmbeddingResult(
            normalized, self.provider_name, self.model, self.revision, self.dimensions,
            True, True, False, (perf_counter() - started) * 1000,
        )

    def embed(self, text: str) -> list[float]:
        return list(self.embed_result(text).vector)
