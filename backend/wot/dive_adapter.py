"""The sole WOT boundary to the pinned event-sourced DIVE memory core.

Callers must establish consent and source origin before reaching this adapter.
The adapter accepts only real-user namespaces and stores utterances as quoted
observations, never as inferred facts or personality traits.
"""

from __future__ import annotations

import re
from pathlib import Path
from threading import RLock

from dive_memory.extraction import Candidate
from dive_memory.gate import GateDecision
from dive_memory.ids import stable_id
from dive_memory.models import MemoryStatus, utc_now
from dive_memory.service import MemoryService
from dive_memory.temporal import require_instant

_NAMESPACE = re.compile(r"user/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(real|self_reports|corrections)\Z")
_SOURCE_TYPES = {"real": "user", "self_reports": "user", "corrections": "user_correction"}


class _VerbatimObservationExtractor:
    """Preserve exact Chinese/English text without semantic promotion."""

    supports_source_type = True

    def extract(self, text, *, observed_at=None, explicit=False, source_type=None):
        if source_type not in {"user", "user_correction"}:
            raise ValueError("Only admitted real-user sources may enter DIVE")
        return [
            Candidate(
                content=text,
                kind="episode",
                evidence_state="OBSERVATION",
                structured_content={
                    "subject": "user",
                    "predicate": "dialogue_observation",
                    "assertion": "reported_dialogue",
                    "role": "user",
                    "retention_policy": "wot-verbatim-observation-v1",
                    "source_span": [0, len(text)],
                },
                decision=GateDecision(
                    True, 0.5, 0.5, 0.5, "medium_term",
                    "Admitted utterance retained as an observation, not a verified fact",
                    policy_version="wot-verbatim-observation-v1",
                ),
                valid_from=observed_at,
            )
        ]


def _namespace(value: str) -> str:
    if not isinstance(value, str) or _NAMESPACE.fullmatch(value) is None:
        raise ValueError("Invalid real-user memory namespace")
    return value


def _opaque(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{field} must be a nonempty identifier of at most 256 characters")
    return value


class DiveMemoryAdapter:
    def __init__(self, db_path: str | Path):
        self._lock = RLock()
        self._service = MemoryService(
            str(db_path), extractor=_VerbatimObservationExtractor(), deterministic_ids=True
        )

    def ingest(self, namespace, text, *, key, message_id, observed_at) -> dict:
        namespace = _namespace(namespace)
        key = _opaque(key, "key")
        message_id = _opaque(message_id, "message_id")
        if not isinstance(text, str) or not text.strip() or len(text) > 32_000:
            raise ValueError("Memory text must contain 1 to 32000 characters")
        if not isinstance(observed_at, str) or not require_instant(observed_at, "observed_at"):
            raise ValueError("observed_at must be a valid nonempty timestamp")
        with self._lock:
            service = self._service
            event_id = stable_id("evt", namespace, key)
            if service.store.is_tombstoned("event", event_id):
                raise ValueError("Deleted source cannot be replayed")
            existing = service.store.get_event_by_idempotency(namespace, key)
            if existing is not None and (
                existing.payload.get("text") != text
                or existing.source_message_id != message_id
                or existing.observed_at != observed_at
            ):
                raise ValueError("Idempotency key already belongs to another source payload")
            if existing is None:
                # The core commits event + outbox before projecting memory. A
                # failure is deliberately surfaced, so the WOT outbox retries
                # the same key rather than reporting a partial write as done.
                try:
                    service.ingest(
                        namespace, text, idempotency_key=key,
                        source_message_id=message_id, observed_at=observed_at,
                        source_type=_SOURCE_TYPES[namespace.rsplit("/", 1)[1]],
                        event_type="wot_real_observation",
                    )
                except Exception:
                    raise RuntimeError("Memory projection failed; retry the same source key") from None
            self._ensure_projected(event_id)
            memories = service.store.memories_for_event(event_id)
            return {
                "event_id": event_id,
                "memory_ids": [m.id for m in memories if m.status != MemoryStatus.DELETED],
            }

    def _ensure_projected(self, event_id: str) -> None:
        # The real core worker owns claim/retry/transaction semantics. Reading
        # its status prevents the legacy duplicate branch (empty memory_ids)
        # from silently acknowledging a previously failed projection.
        for _ in range(32):
            row = self._service.store.db.execute(
                "SELECT status, attempts FROM outbox WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is not None and row["status"] == "DONE":
                return
            if row is None or row["attempts"] >= 3:
                raise RuntimeError("Memory projection unavailable; source remains pending")
            results = self._service.process_pending(limit=1000)
            target = next((r for r in results if r["event_id"] == event_id), None)
            if target is not None and "error" in target:
                raise RuntimeError("Memory projection failed; source remains pending")
            if not results:
                break
        raise RuntimeError("Memory projection unavailable; source remains pending")

    def retrieve(self, namespace, query, limit=12) -> list[dict]:
        namespace = _namespace(namespace)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("query must contain 1 to 4000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self._lock:
            result = self._service.retrieve(namespace, query, limit=limit)
            return [
                {
                    "memory_id": item.memory.id,
                    "event_ids": list(item.memory.source_event_ids),
                    "text": item.memory.content,
                    "evidence_state": item.memory.evidence_state.value,
                    "observed_at": item.memory.observed_at,
                    "namespace": item.memory.namespace,
                }
                for item in result.items
            ]

    def delete(self, namespace, event_id) -> None:
        namespace = _namespace(namespace)
        event_id = _opaque(event_id, "event_id")
        with self._lock:
            event = self._service.get_event(event_id)
            if event is None:
                return
            if event.namespace != namespace:
                raise KeyError("Source is unavailable in this namespace")
            self._service.forget_event(event_id, hard=True)

    def delete_by_key(self, namespace, key) -> None:
        namespace = _namespace(namespace)
        key = _opaque(key, "key")
        with self._lock:
            event = self._service.store.get_event_by_idempotency(namespace, key)
            if event is not None:
                self._service.forget_event(event.id, hard=True)
            else:
                # A WOT deletion may win the race before the core append, or
                # after append but before WOT saves event_id. Keep only the
                # deterministic opaque tombstone, never the deleted text/key.
                with self._service.store.transaction():
                    self._service.store.db.execute(
                        "INSERT OR IGNORE INTO tombstones"
                        "(object_type,object_id,reason,deleted_at,namespace) "
                        "VALUES ('event',?,'wot_source_deleted',?,?)",
                        (stable_id("evt", namespace, key), utc_now(), namespace),
                    )

    def close(self) -> None:
        with self._lock:
            self._service.store.close()
