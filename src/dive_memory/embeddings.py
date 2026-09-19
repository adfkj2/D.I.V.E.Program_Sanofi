from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .ids import stable_vector


def _normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class DeterministicEmbeddingProvider:
    """Offline embedding used by the local MVP and deterministic tests."""

    dimensions: int = 96
    model: str = "deterministic-sha256-v1"

    def embed(self, text: str) -> list[float]:
        return stable_vector(text, dimensions=self.dimensions)


@dataclass(slots=True)
class OpenAICompatibleEmbeddingProvider:
    """Embedding adapter for OpenAI-compatible ``/embeddings`` endpoints."""

    endpoint: str
    model: str
    api_key: str
    dimensions: int
    timeout_seconds: float = 30.0
    fallback: EmbeddingProvider | None = None

    def embed(self, text: str) -> list[float]:
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
            return _normalise(vector)
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            if self.fallback is None:
                raise RuntimeError("embedding provider request failed") from None
            return self.fallback.embed(text)
