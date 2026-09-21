from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Mapping, Any


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field} must be a boolean")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: str = "development"
    trace_enabled: bool = True
    allow_sensitive_trace_payloads: bool = False
    require_authorizer: bool | None = None
    require_semantic_embeddings: bool | None = None
    version: str = "runtime-config-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"development", "test", "production"}:
            raise ValueError("mode must be development, test, or production")
        if self.mode == "production" and self.allow_sensitive_trace_payloads:
            raise ValueError("production cannot enable raw sensitive trace payloads")

    @property
    def authorizer_required(self) -> bool:
        return self.mode == "production" if self.require_authorizer is None else self.require_authorizer

    @property
    def semantic_embeddings_required(self) -> bool:
        return self.mode == "production" if self.require_semantic_embeddings is None else self.require_semantic_embeddings

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RuntimeConfig":
        allowed = {
            "mode", "trace_enabled", "allow_sensitive_trace_payloads",
            "require_authorizer", "require_semantic_embeddings", "version",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown runtime configuration: {sorted(unknown)!r}")
        converted = dict(values)
        for field in (
            "trace_enabled", "allow_sensitive_trace_payloads", "require_authorizer",
            "require_semantic_embeddings",
        ):
            if field in converted and converted[field] is not None:
                converted[field] = _boolean(converted[field], field)
        return cls(**converted)

    def sha256(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()

    def validate_dependencies(self, *, authorizer: object | None, embedder: object | None) -> None:
        if self.authorizer_required and authorizer is None:
            raise RuntimeError("production runtime requires an authorizer")
        if self.semantic_embeddings_required and not bool(getattr(embedder, "semantic_similarity", False)):
            raise RuntimeError("production runtime requires a healthy semantic embedding provider")
