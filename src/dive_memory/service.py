from __future__ import annotations

import json
import threading
from functools import wraps
from pathlib import Path

from .extraction import extract_candidates
from .ids import new_id
from .lifecycle import ConsolidationReport, archive_expired, consolidate
from .models import Event, EvidenceState, Memory, MemoryStatus, RetrievalItem, RetrievalResult, utc_now
from .context import pack_context
from .planner import plan_query
from .llm import ExtractionProvider, HeuristicExtractionProvider
from .entities import entity_candidates
from .relations import relation_predicate
from .decay import apply_decay
from .store import SQLiteStore
from .embeddings import EmbeddingProvider


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class MemoryService:
    def __init__(self, db_path: str = ":memory:", extractor: ExtractionProvider | None = None,
                 embedder: EmbeddingProvider | None = None) -> None:
        self.store = SQLiteStore(db_path, embedder=embedder)
        self._lock = threading.RLock()
        self._memory_enabled: dict[str, bool] = {}
        self.extractor = extractor or HeuristicExtractionProvider()

    @_synchronized
    def ingest(self, namespace: str, text: str, *, event_type: str = "message", explicit: bool = False,
               observed_at: str | None = None, idempotency_key: str | None = None, defer: bool = False) -> dict:
        with self._lock:
            observed_at = observed_at or utc_now()
            event = Event(new_id("evt"), namespace, event_type, {"text": text, "explicit": explicit}, observed_at, idempotency_key=idempotency_key)
            accepted = self.store.append_event(event)
            if not accepted:
                existing = self.store.get_event_by_idempotency(idempotency_key) if idempotency_key else None
                return {"event_id": existing.id if existing else event.id, "accepted": False, "duplicate": True, "memory_ids": []}
            if self._memory_enabled.get(namespace, True) is False:
                # The event remains auditable, but it has been intentionally
                # rejected for memory projection and must not be retried later.
                self.mark_event_processed(event.id)
                return {"event_id": event.id, "accepted": True, "duplicate": False, "memory_ids": [], "memory_disabled": True}
            if defer:
                return {"event_id": event.id, "accepted": True, "duplicate": False, "memory_ids": [], "deferred": True}
            memory_ids = self._process_event(event, text=text, explicit=explicit)
            self.mark_event_processed(event.id)
            return {"event_id": event.id, "accepted": True, "duplicate": False, "memory_ids": memory_ids}

    def _process_event(self, event: Event, *, text: str | None = None, explicit: bool = False) -> list[str]:
        text = text if text is not None else str(event.payload.get("text", ""))
        explicit = explicit or bool(event.payload.get("explicit", False))
        memory_ids: list[str] = []
        for candidate in self.extractor.extract(text, explicit=explicit, observed_at=event.observed_at):
            memory = Memory(new_id("mem"), event.namespace, candidate.kind, EvidenceState(candidate.evidence_state), candidate.content,
                            candidate.structured_content, MemoryStatus.ACTIVE, candidate.decision.importance,
                            candidate.decision.confidence, candidate.decision.salience, candidate.decision.durability,
                            candidate.valid_from, candidate.valid_to, event.observed_at, source_event_ids=[event.id])
            existing = self._find_conflict(memory)
            if existing:
                self.store.close_validity(existing.id, event.observed_at)
                self.store.update_status(existing.id, MemoryStatus.SUPERSEDED)
                memory.supersedes_id = existing.id
            self.store.add_memory(memory)
            for entity_name, entity_type in entity_candidates(memory.content, memory.structured_content):
                self.store.attach_entity(event.namespace, memory.id, entity_name, entity_type)
            predicate = relation_predicate(str(memory.structured_content.get("predicate", "")))
            if predicate:
                self.store.add_relation(event.namespace, memory.id, f"user:{event.namespace}", predicate,
                                        str(memory.structured_content.get("value", "")), memory.valid_from,
                                        memory.valid_to, memory.confidence, event.id)
                self._refresh_profile(event.namespace)
            memory_ids.append(memory.id)
        return memory_ids

    def _find_conflict(self, memory: Memory) -> Memory | None:
        predicate = memory.structured_content.get("predicate")
        value = memory.structured_content.get("value")
        for existing in self.store.active_memories(memory.namespace):
            if existing.kind != memory.kind or existing.structured_content.get("predicate") != predicate:
                continue
            if existing.structured_content.get("value") != value:
                return existing
        return None

    @_synchronized
    def retrieve(self, namespace: str, query: str, *, limit: int = 8, as_of: str | None = None,
                 intent: str | None = None) -> RetrievalResult:
        with self._lock:
            query_plan = plan_query(query, limit=limit)
            intent = intent or query_plan.intent
            as_of = as_of or query_plan.as_of
            include_history = query_plan.temporal_mode in {"historical", "earliest", "all"} or as_of is not None
            rows = self.store.search(namespace, query, limit=limit, as_of=as_of, include_history=include_history)
            items = [RetrievalItem(memory, score, channels, memory.source_event_ids) for memory, score, channels in rows]
            for rank, item in enumerate(items, start=1):
                self.store.record_access(item.memory.id, query, rank, item.score, utc_now())
            abstain = None if items else "no supported memory found"
            return RetrievalResult(items, {"intent": intent, "temporal_mode": query_plan.temporal_mode,
                                           "as_of": as_of, "limit": query_plan.limit,
                                           "token_budget": query_plan.token_budget, "channels": query_plan.channels}, abstain)

    @_synchronized
    def retrieve_context(self, namespace: str, query: str, *, limit: int = 8, token_budget: int = 1500,
                         as_of: str | None = None) -> dict:
        result = self.retrieve(namespace, query, limit=limit, as_of=as_of)
        packed = pack_context(result.items, token_budget)
        return {"context": packed.text, "items": packed.items, "estimated_tokens": packed.estimated_tokens,
                "omitted": packed.omitted, "plan": result.plan, "abstain_reason": result.abstain_reason}

    @_synchronized
    def forget(self, memory_id: str, reason: str = "user_request") -> None:
        memory = self.get_memory(memory_id)
        self.store.tombstone_memory(memory_id, reason, utc_now())
        if memory is not None:
            self._refresh_profile(memory.namespace)

    @_synchronized
    def forget_event(self, event_id: str, reason: str = "user_request") -> list[str]:
        namespaces = {memory.namespace for memory in self.store.memories_for_event(event_id)}
        deleted = self.store.tombstone_event(event_id, reason, utc_now())
        for namespace in namespaces:
            self._refresh_profile(namespace)
        return deleted

    @_synchronized
    def set_memory_enabled(self, namespace: str, enabled: bool) -> None:
        self._memory_enabled[namespace] = enabled

    @_synchronized
    def pending_events(self, limit: int = 100) -> list[Event]:
        return self.store.pending_events(limit)

    @_synchronized
    def mark_event_processed(self, event_id: str) -> None:
        self.store.mark_event_processed(event_id, utc_now())

    @_synchronized
    def process_pending(self, limit: int = 100) -> list[dict]:
        results = []
        for event in self.pending_events(limit):
            memory_ids = self._process_event(event)
            self.mark_event_processed(event.id)
            results.append({"event_id": event.id, "memory_ids": memory_ids})
        return results

    @_synchronized
    def get_memory(self, memory_id: str) -> Memory | None:
        with self._lock:
            return self.store.get_memory(memory_id)

    @_synchronized
    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            return self.store.get_event(event_id)

    @_synchronized
    def list_memories(self, namespace: str, *, status: str | None = None, limit: int = 100,
                      offset: int = 0) -> list[Memory]:
        memories = self.store.memories_for_namespace(namespace, include_deleted=status == "DELETED")
        if status:
            memories = [memory for memory in memories if memory.status.value == status]
        return memories[offset:offset + max(1, min(limit, 1000))]

    @_synchronized
    def profile(self, namespace: str) -> dict:
        return self.store.get_profile(namespace) or {"_meta": {"schema_version": 1, "generated_from_version": 0}}

    def _refresh_profile(self, namespace: str) -> None:
        profile: dict = {}
        version = 0
        for memory in self.store.active_memories(namespace):
            predicate = memory.structured_content.get("predicate")
            value = memory.structured_content.get("value")
            if predicate and value is not None:
                profile[predicate] = value
                version = max(version, memory.version)
        self.store.upsert_profile(namespace, profile, version, utc_now())

    @_synchronized
    def correct_memory(self, memory_id: str, content: str, *, namespace: str | None = None) -> dict:
        old = self.get_memory(memory_id)
        if old is None:
            raise KeyError(memory_id)
        self.forget(memory_id, reason="user_correction")
        return self.ingest(namespace or old.namespace, content, explicit=True, event_type="memory_correction")

    @_synchronized
    def consolidate(self, namespace: str, *, dry_run: bool = False) -> ConsolidationReport:
        return consolidate(self.store, namespace, dry_run=dry_run)

    @_synchronized
    def archive_expired(self, namespace: str, *, now: str | None = None, dry_run: bool = False) -> int:
        return archive_expired(self.store, namespace, now=now, dry_run=dry_run)

    @_synchronized
    def decay(self, namespace: str, *, now: str | None = None, dry_run: bool = False) -> dict:
        result = apply_decay(self.store, namespace, now=now, dry_run=dry_run)
        self._refresh_profile(namespace)
        return result

    @_synchronized
    def timeline(self, namespace: str, *, as_of: str | None = None) -> list[Memory]:
        result = []
        for memory in self.store.memories_for_namespace(namespace):
            if as_of and memory.valid_from and memory.valid_from > as_of:
                continue
            if as_of and memory.valid_to and memory.valid_to <= as_of:
                continue
            result.append(memory)
        return result

    @_synchronized
    def export_namespace(self, namespace: str, path: str | None = None) -> dict:
        payload = {
            "namespace": namespace,
            "events": [event.__dict__ if hasattr(event, "__dict__") else {
                "id": event.id, "event_type": event.event_type, "payload": event.payload,
                "observed_at": event.observed_at, "occurred_from": event.occurred_from,
                "occurred_to": event.occurred_to, "created_at": event.created_at,
            } for event in self.store.events_for_namespace(namespace)],
            "memories": [{"id": memory.id, "kind": memory.kind, "content": memory.content,
                          "structured_content": memory.structured_content, "status": memory.status.value,
                          "evidence_state": memory.evidence_state.value, "valid_from": memory.valid_from,
                          "valid_to": memory.valid_to, "source_event_ids": memory.source_event_ids}
                         for memory in self.store.memories_for_namespace(namespace, include_deleted=True)],
        }
        if path:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
