from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import temporal
from .models import Memory, MemoryStatus
from .normalization import normalize_text
from .store import SQLiteStore


@dataclass(frozen=True, slots=True)
class ConsolidationChange:
    """One deterministic, reviewable projection change.

    Consolidation is allowed to retire a duplicate projection, never to invent
    or rewrite a fact.  Keeping the exact identities and source events in the
    report makes both dry-runs and committed jobs independently auditable.
    """

    survivor_id: str
    duplicate_id: str
    subject: str
    predicate: str
    value: str
    valid_from: str | None
    valid_to: str | None
    source_event_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "action": "MERGE_PROVENANCE",
            "survivor_id": self.survivor_id,
            "duplicate_id": self.duplicate_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "source_event_ids": list(self.source_event_ids),
            "reason": "exact same-subject fact within the same validity window",
        }


@dataclass(slots=True)
class ConsolidationReport:
    namespace: str
    examined: int
    merged: int
    archived: int
    dry_run: bool
    changes: tuple[ConsolidationChange, ...] = field(default_factory=tuple)


def _exact_duplicate_key(memory: Memory) -> tuple[str, ...] | None:
    """Return a tenant- and time-safe exact-duplicate key.

    Observed time is deliberately omitted: two observations of the same fact
    are useful corroborating provenance.  Valid time is included because two
    otherwise identical facts that held in different periods are history, not
    duplicates.  Unstructured memories are never merged by this maintenance
    rule.
    """

    structured = memory.structured_content or {}
    predicate = normalize_text(str(structured.get("predicate", "")), casefold=True)
    raw_value = structured.get("normalized_value", structured.get("value"))
    if not predicate or raw_value is None:
        return None
    value = normalize_text(str(raw_value), casefold=True)
    if not value:
        return None
    subject = normalize_text(str(structured.get("subject", "user")), casefold=True)
    return (
        memory.namespace,
        subject,
        predicate,
        value,
        memory.kind,
        memory.evidence_state.value,
        memory.valid_from or "",
        memory.valid_to or "",
    )


def consolidate(store: SQLiteStore, namespace: str, *, dry_run: bool = False) -> ConsolidationReport:
    # ``active_memories`` is ordered, so the surviving copy of a duplicate is
    # deterministic instead of following SQLite's row order.
    memories = store.active_memories(namespace)
    seen: dict[tuple[str, ...], Memory] = {}
    merged = 0
    changes: list[ConsolidationChange] = []
    for memory in memories:
        key = _exact_duplicate_key(memory)
        if key is None:
            continue
        if key in seen:
            survivor = seen[key]
            merged += 1
            source_event_ids = tuple(dict.fromkeys(
                [*survivor.source_event_ids, *memory.source_event_ids]
            ))
            changes.append(ConsolidationChange(
                survivor_id=survivor.id,
                duplicate_id=memory.id,
                subject=key[1],
                predicate=key[2],
                value=key[3],
                valid_from=memory.valid_from,
                valid_to=memory.valid_to,
                source_event_ids=source_event_ids,
            ))
            if not dry_run:
                store.merge_memory(survivor.id, memory.id)
        else:
            seen[key] = memory
    return ConsolidationReport(namespace, len(memories), merged, 0, dry_run, tuple(changes))


def archive_expired(store: SQLiteStore, namespace: str, *, now: str | None = None, dry_run: bool = False) -> int:
    now = now or datetime.now(timezone.utc).isoformat()
    count = 0
    for memory in store.active_memories(namespace):
        # A real datetime comparison: the previous string comparison treated a
        # date-only bound ("2025-01-15") as later than the full timestamp it was
        # being compared against.
        if temporal.expired(memory.valid_to, now):
            count += 1
            if not dry_run:
                store.update_status(memory.id, MemoryStatus.ARCHIVED)
    return count
