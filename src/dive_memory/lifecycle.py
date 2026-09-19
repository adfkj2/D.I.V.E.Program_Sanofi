from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import MemoryStatus
from .store import SQLiteStore


@dataclass(slots=True)
class ConsolidationReport:
    namespace: str
    examined: int
    merged: int
    archived: int
    dry_run: bool


def consolidate(store: SQLiteStore, namespace: str, *, dry_run: bool = False) -> ConsolidationReport:
    memories = store.active_memories(namespace)
    seen: dict[tuple[str, str], str] = {}
    merged = 0
    for memory in memories:
        key = (memory.structured_content.get("predicate", ""), memory.structured_content.get("value", ""))
        if key in seen:
            merged += 1
            if not dry_run:
                store.update_status(memory.id, MemoryStatus.MERGED)
        else:
            seen[key] = memory.id
    return ConsolidationReport(namespace, len(memories), merged, 0, dry_run)


def archive_expired(store: SQLiteStore, namespace: str, *, now: str | None = None, dry_run: bool = False) -> int:
    now = now or datetime.now(timezone.utc).isoformat()
    count = 0
    for memory in store.active_memories(namespace):
        if memory.valid_to and memory.valid_to <= now:
            count += 1
            if not dry_run:
                store.update_status(memory.id, MemoryStatus.ARCHIVED)
    return count
