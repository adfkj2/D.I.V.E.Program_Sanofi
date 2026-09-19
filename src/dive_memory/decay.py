from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import temporal
from .models import MemoryStatus
from .store import SQLiteStore


# Only *temporary* durabilities expire on a timer. docs/04 is explicit that
# stable identity, explicit long-term preferences and user-pinned items carry no
# automatic TTL, and expiry of temporary plans is driven by the validity window
# handled in ``lifecycle.archive_expired``.
TTL_DAYS = {"ephemeral": 1, "short_term": 14, "medium_term": 90}


def apply_decay(store: SQLiteStore, namespace: str, *, now: str | None = None, dry_run: bool = False) -> dict:
    # ``parse_instant`` treats naive input as UTC, so a caller passing
    # "2025-02-01T00:00:00" no longer raises
    # "can't compare offset-naive and offset-aware datetimes".
    now_dt = temporal.parse_instant(now) or datetime.now(timezone.utc)
    archived = 0
    weakened = 0
    for memory in store.active_memories(namespace):
        days = TTL_DAYS.get(memory.durability)
        if not days or not memory.observed_at:
            continue
        observed = temporal.parse_instant(memory.observed_at)
        if observed is None:
            continue
        if observed <= now_dt - timedelta(days=days):
            archived += 1
            if not dry_run:
                store.update_status(memory.id, MemoryStatus.ARCHIVED)
        elif memory.durability == "medium_term" and not dry_run:
            store.update_importance(memory.id, max(0.2, memory.importance * 0.95))
            weakened += 1
    return {"namespace": namespace, "archived": archived, "weakened": weakened, "dry_run": dry_run}
