from __future__ import annotations

import hashlib
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def stable_vector(text: str, dimensions: int = 96) -> list[float]:
    """Dependency-free deterministic embedding for local tests.

    Production adapters should replace this with a real embedding model and keep
    the model version in the index metadata.
    """
    values = [0.0] * dimensions
    tokens = text.lower().split()
    if not tokens:
        return values
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for i in range(0, min(len(digest), dimensions), 2):
            index = int.from_bytes(digest[i : i + 2], "big") % dimensions
            values[index] += 1.0 if digest[i] & 1 else -1.0
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
