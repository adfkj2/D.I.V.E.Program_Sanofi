from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvidenceState(StrEnum):
    FACT = "FACT"
    OBSERVATION = "OBSERVATION"
    INFERENCE = "INFERENCE"


class MemoryStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    MERGED = "MERGED"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class Event:
    id: str
    namespace: str
    event_type: str
    payload: dict[str, Any]
    observed_at: str
    occurred_from: str | None = None
    occurred_to: str | None = None
    source_message_id: str | None = None
    idempotency_key: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Memory:
    id: str
    namespace: str
    kind: str
    evidence_state: EvidenceState
    content: str
    structured_content: dict[str, Any]
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: float = 0.5
    confidence: float = 0.5
    salience: float = 0.5
    durability: str = "medium_term"
    valid_from: str | None = None
    valid_to: str | None = None
    observed_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    version: int = 1
    source_event_ids: list[str] = field(default_factory=list)
    supersedes_id: str | None = None
    contradicts_id: str | None = None


@dataclass(slots=True)
class RetrievalItem:
    memory: Memory
    score: float
    channels: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalResult:
    items: list[RetrievalItem]
    plan: dict[str, Any]
    abstain_reason: str | None = None
    degraded: bool = False
