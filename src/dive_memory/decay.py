from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import MemoryStatus
from .store import SQLiteStore


TTL_DAYS = {"ephemeral": 1, "short_term": 14, "medium_term": 90}


def apply_decay(store: SQLiteStore, namespace: str, *, now: str | None = None, dry_run: bool = False) -> dict:
    now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    archived = 0
    weakened = 0
    for memory in store.active_memories(namespace):
        days = TTL_DAYS.get(memory.durability)
        if not days or not memory.observed_at:
            continue
        observed = datetime.fromisoformat(memory.observed_at)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if observed <= now_dt - timedelta(days=days):
            archived += 1
            if not dry_run:
                store.update_status(memory.id, MemoryStatus.ARCHIVED)
        elif memory.durability == "medium_term" and not dry_run:
            store.update_importance(memory.id, max(0.2, memory.importance * 0.95))
            weakened += 1
    return {"namespace": namespace, "archived": archived, "weakened": weakened, "dry_run": dry_run}
