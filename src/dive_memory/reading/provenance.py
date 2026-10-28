"""Metadata for already admitted source snapshots, independent of any benchmark.

The adapter reads source ownership, declared role and observation clock. It never
reads an event's text or treats a source's event date as its statement date.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..dialogue_retention import USER_SOURCE_TYPES
from ..models import Event, Memory, MemoryStatus
from ..temporal import parse_instant, require_instant


@dataclass(frozen=True, slots=True)
class SourceStamp:
    source_ref: str
    role: str = "unknown"
    statement_at: str | None = None
    source_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise ValueError("source stamp requires a source reference")
        if not isinstance(self.role, str) or self.role not in {
            "user",
            "assistant",
            "unknown",
        }:
            raise ValueError("source stamp role must be user, assistant or unknown")
        if self.statement_at is not None:
            if not isinstance(self.statement_at, str) or not self.statement_at.strip():
                raise ValueError("source statement clock must be a timestamp")
            require_instant(self.statement_at, "statement_at")
        if self.source_message_id is not None and (
            not isinstance(self.source_message_id, str)
            or not self.source_message_id.strip()
        ):
            raise ValueError("source message ID must be an opaque nonempty string")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "source_ref": self.source_ref,
            "role": self.role,
            "statement_at": self.statement_at,
        }
        if self.source_message_id is not None:
            result["source_message_id"] = self.source_message_id
        return result


class ProvenanceStore(Protocol):
    def is_tombstoned(self, object_type: str, object_id: str) -> bool: ...

    def memory_enabled(self, namespace: str) -> bool: ...


class ProvenanceService(Protocol):
    @property
    def store(self) -> ProvenanceStore: ...

    def get_memory(self, memory_id: str) -> Memory | None: ...

    def get_event(self, event_id: str) -> Event | None: ...


class EvidenceProvenanceProvider(Protocol):
    def for_memory(
        self,
        memory: Memory,
        source_refs: Sequence[str],
        *,
        namespace: str,
        as_of: str | None = None,
    ) -> tuple[SourceStamp, ...]: ...


def admitted_memory(
    service: ProvenanceService,
    memory: Memory,
    *,
    namespace: str,
    as_of: str | None = None,
) -> Memory:
    """Resolve current admission metadata without accepting a caller's snapshot."""
    if not service.store.memory_enabled(namespace):
        raise ValueError("memory is disabled in this project")
    cutoff = parse_instant(require_instant(as_of, "as_of"))
    current = service.get_memory(memory.id)
    if (
        current is None
        or current.namespace != namespace
        or memory.namespace != namespace
        or current.status
        in {
            MemoryStatus.CANDIDATE,
            MemoryStatus.REJECTED,
            MemoryStatus.DELETED,
            MemoryStatus.MERGED,
        }
        or service.store.is_tombstoned("memory", memory.id)
    ):
        raise ValueError("memory is unavailable in this project")
    if current.version != memory.version:
        raise ValueError("memory snapshot version is no longer current")
    if cutoff is not None:
        for value in (current.valid_from, current.observed_at):
            parsed = parse_instant(value)
            if value and (parsed is None or parsed > cutoff):
                raise ValueError("memory is outside the observation cutoff")
    return current


def complete_source_refs(memory: Memory, source_refs: Sequence[str]) -> tuple[str, ...]:
    if isinstance(source_refs, (str, bytes)) or any(
        not isinstance(ref, str) or not ref.strip() for ref in source_refs
    ):
        raise ValueError("source references must be nonempty strings")
    refs = tuple(dict.fromkeys(source_refs))
    if set(refs) != set(memory.source_event_ids):
        raise ValueError("source references differ from current memory provenance")
    return refs


@dataclass(frozen=True, slots=True)
class MemoryServiceProvenanceProvider:
    service: ProvenanceService

    def for_memory(
        self,
        memory: Memory,
        source_refs: Sequence[str],
        *,
        namespace: str,
        as_of: str | None = None,
    ) -> tuple[SourceStamp, ...]:
        current = admitted_memory(
            self.service, memory, namespace=namespace, as_of=as_of
        )
        refs = complete_source_refs(current, source_refs)
        cutoff = parse_instant(require_instant(as_of, "as_of"))
        result = []
        for ref in refs:
            unknown = SourceStamp(ref)
            if self.service.store.is_tombstoned("event", ref):
                result.append(unknown)
                continue
            event = self.service.get_event(ref)
            if event is None or event.namespace != namespace:
                result.append(unknown)
                continue
            observed = parse_instant(event.observed_at)
            if observed is None or (cutoff is not None and observed > cutoff):
                result.append(unknown)
                continue
            declared = event.payload.get("source_type")
            source_type = (
                declared.strip().casefold() if isinstance(declared, str) else ""
            )
            role = (
                "user"
                if source_type in USER_SOURCE_TYPES
                else "assistant"
                if source_type == "assistant"
                else "unknown"
            )
            message_id = event.source_message_id
            result.append(
                SourceStamp(
                    ref,
                    role,
                    event.observed_at,
                    message_id
                    if isinstance(message_id, str) and message_id.strip()
                    else None,
                )
            )
        return tuple(result)
