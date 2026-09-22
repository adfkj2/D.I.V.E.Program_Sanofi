from __future__ import annotations

import json
from hashlib import sha256
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from .ids import new_id, stable_id
from .lifecycle import ConsolidationReport, archive_expired, consolidate
from .models import Event, EvidenceState, Memory, MemoryStatus, RetrievalItem, RetrievalResult, utc_now
from . import temporal
from .context import pack_context
from .planner import plan_query
from .llm import ExtractionOutcome, ExtractionOutcomeCode, ExtractionProvider, HeuristicExtractionProvider
from .gate import decide
from .entities import entity_candidates, is_user_subject
from .relations import is_single_valued, relation_predicate
from .resolution import (
    RelationshipResolver,
    ResolutionAction,
    ResolutionContext,
    _AUTHORITATIVE_SOURCES,
)
from .retrieval import RetrievalConfig
from .token_budget import TokenCounter
from .decay import apply_decay
from .store import SQLiteStore
from .embeddings import EmbeddingProvider


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _accepts_source_type(extractor: Any) -> bool:
    """True when the extractor advertises support for the ``source_type`` kwarg.

    The shipped providers set ``supports_source_type = True``. Custom
    extractors (and every pre-v2 implementation) do not, so the service calls
    them with exactly the arguments they were written for.
    """
    return bool(getattr(extractor, "supports_source_type", False))


def _extractor_source_kwargs(extractor: Any, source_type: str | None) -> dict[str, Any]:
    return {"source_type": source_type} if _accepts_source_type(extractor) else {}


class MemoryService:
    def __init__(self, db_path: str = ":memory:", extractor: ExtractionProvider | None = None,
                 embedder: EmbeddingProvider | None = None, store: Any | None = None,
                 gate: Any | None = None) -> None:
        self.store = store or SQLiteStore(db_path, embedder=embedder)
        self._lock = threading.RLock()
        # ``gate`` is opt-in: passing a gate (e.g. ``semantic_gate.SemanticGate``)
        # selects the v2 policy; omitting it keeps the v1 keyword provider that
        # every existing call site expects.
        self.extractor = extractor or HeuristicExtractionProvider(gate=gate)
        self.resolver = RelationshipResolver()

    @_synchronized
    def ingest(self, namespace: str, text: str, *, event_type: str = "message", explicit: bool = False,
               observed_at: str | None = None, idempotency_key: str | None = None, defer: bool = False,
               supersedes_memory_id: str | None = None, occurred_from: str | None = None,
               occurred_to: str | None = None, source_message_id: str | None = None,
               source_type: str | None = None) -> dict:
        observed_at = temporal.require_instant(observed_at, "observed_at") or utc_now()
        occurred_from = temporal.require_instant(occurred_from, "occurred_from")
        occurred_to = temporal.require_instant(occurred_to, "occurred_to")
        if occurred_from and occurred_to and temporal.parse_instant(occurred_from) >= temporal.parse_instant(occurred_to):
            raise ValueError("occurred_from must be earlier than occurred_to")
        payload = {"text": text, "explicit": explicit}
        # ``source_type`` is the provenance tier the v2 write gate needs to
        # distinguish a user-supplied fact from an untrusted web string or a
        # generated summary. It rides in the payload so no schema migration is
        # required and old events (which lack the key) keep their v1 semantics.
        if source_type is not None:
            payload["source_type"] = str(source_type)
        if supersedes_memory_id:
            payload["supersedes_memory_id"] = supersedes_memory_id
        event = Event(new_id("evt"), namespace, event_type, payload, observed_at,
                      occurred_from=occurred_from, occurred_to=occurred_to,
                      source_message_id=source_message_id, idempotency_key=idempotency_key)
        accepted = self.store.append_event(event)
        if not accepted:
            existing = self.store.get_event_by_idempotency(namespace, idempotency_key) if idempotency_key else None
            existing_memories = self.store.memories_for_event(existing.id) if existing else []
            return {"event_id": existing.id if existing else event.id, "accepted": False, "duplicate": True,
                    "memory_ids": [memory.id for memory in existing_memories]}
        if not self.store.memory_enabled(namespace):
            # The event remains auditable, but it has been intentionally
            # rejected for memory projection and must not be retried later.
            with self.store.transaction():
                self.store.mark_event_processed(event.id, utc_now())
            return {"event_id": event.id, "accepted": True, "duplicate": False, "memory_ids": [],
                    "memory_disabled": True}
        if defer:
            return {"event_id": event.id, "accepted": True, "duplicate": False, "memory_ids": [], "deferred": True}
        try:
            # Memory, provenance, entity/relation/profile projections and the
            # DONE outbox transition form one atomic unit. The event itself was
            # already committed and remains retryable if projection fails.
            with self.store.transaction():
                memory_ids = self._process_event(event, text=text, explicit=explicit)
                self.store.mark_event_processed(event.id, utc_now())
        except Exception as exc:
            self.store.mark_event_failed(event.id, f"{type(exc).__name__}: {exc}")
            raise
        return {"event_id": event.id, "accepted": True, "duplicate": False, "memory_ids": memory_ids}

    def _memory_source_type(self, memory: Memory) -> str | None:
        """Recover the provenance tier of an existing memory.

        Each memory records the events that produced it; those events carry the
        ``source_type`` that was supplied at ingest time. A memory reinforced by
        several events has no single tier, so only an unambiguous first
        (earliest recorded) event is reported. Returning ``None`` means
        "unknown", which every caller must treat as the pre-v2 default.
        """
        tiers = set()
        for event_id in memory.source_event_ids:
            event = self.store.get_event(event_id)
            if event is None:
                continue
            tiers.add(event.payload.get("source_type"))
        if len(tiers) != 1:
            return None
        tier = tiers.pop()
        return str(tier) if tier is not None else None

    def _process_event(self, event: Event, *, text: str | None = None, explicit: bool = False) -> list[str]:
        text = text if text is not None else str(event.payload.get("text", ""))
        explicit = explicit or bool(event.payload.get("explicit", False))
        # Provenance tier recorded at ingest time. Events written before v2 (or
        # by callers that never set it) yield ``None``, which the v1 gate never
        # sees and the v2 gate treats as untrusted.
        source_type = event.payload.get("source_type")
        source_type = str(source_type) if source_type is not None else None
        memory_ids: list[str] = []
        forced_id = event.payload.get("supersedes_memory_id")
        forced = self.store.get_memory(str(forced_id)) if forced_id else None
        if forced is not None and forced.namespace != event.namespace:
            raise ValueError("a correction cannot move a memory across namespaces")
        extract_outcome = getattr(self.extractor, "extract_outcome", None)
        if callable(extract_outcome):
            if _accepts_source_type(self.extractor):
                outcome = extract_outcome(
                    text, explicit=explicit, observed_at=event.occurred_from or event.observed_at,
                    source_type=source_type)
            else:
                # Third-party extractors written before v2 keep their exact
                # signature; only providers that opt in receive provenance.
                outcome = extract_outcome(
                    text, explicit=explicit, observed_at=event.occurred_from or event.observed_at)
            candidates = list(outcome.candidates)
        else:
            candidates = self.extractor.extract(
                text, explicit=explicit, observed_at=event.occurred_from or event.observed_at,
                **_extractor_source_kwargs(self.extractor, source_type))
            outcome = ExtractionOutcome(
                ExtractionOutcomeCode.COMMITTED if candidates else ExtractionOutcomeCode.EMPTY,
                tuple(candidates),
                "legacy extraction provider",
                type(self.extractor).__name__,
                type(self.extractor).__name__,
                "legacy",
                "legacy-candidate",
            )
        extractor_version = type(self.extractor).__name__
        if not candidates:
            reason = outcome.reason or "extractor returned no accepted candidates"
            self.store.record_write_decision(
                event.id, -1, accepted=False, importance=0.0, confidence=0.0, salience=0.0,
                durability="ephemeral", reason=reason,
                extractor_version=extractor_version, processed_at=utc_now(),
                outcome_code=outcome.code.value, features={},
                prompt_version=outcome.prompt_version, model_version=outcome.model,
                schema_version=outcome.schema_version,
            )
        for index, candidate in enumerate(candidates):
            self.store.record_write_decision(
                event.id, index, accepted=True, importance=candidate.decision.importance,
                confidence=candidate.decision.confidence, salience=candidate.decision.salience,
                durability=candidate.decision.durability, reason=candidate.decision.reason,
                extractor_version=extractor_version, processed_at=utc_now(),
                outcome_code=outcome.code.value, features=candidate.decision.features,
                policy_version=candidate.decision.policy_version,
                prompt_version=outcome.prompt_version, model_version=outcome.model,
                schema_version=outcome.schema_version,
            )
            memory_id = stable_id("mem", event.id, str(index))
            if self.store.is_tombstoned("memory", memory_id):
                continue
            memory = Memory(memory_id, event.namespace, candidate.kind, EvidenceState(candidate.evidence_state), candidate.content,
                            candidate.structured_content, MemoryStatus.ACTIVE, candidate.decision.importance,
                            candidate.decision.confidence, candidate.decision.salience, candidate.decision.durability,
                            candidate.valid_from, candidate.valid_to, event.observed_at, source_event_ids=[event.id],
                            model_version=self.store.embedder.model,
                            extractor_version=extractor_version)
            if memory.valid_to is None:
                memory.valid_to = event.occurred_to
            existing = forced if index == 0 and forced is not None else self.store.find_related_memory(
                memory, allow_different_value=is_single_valued(memory.structured_content.get("predicate")))
            proposal = self.resolver.classify(
                existing, memory,
                ResolutionContext(
                    event.event_type, text,
                    forced=forced is not None and index == 0,
                    source_type=source_type,
                    existing_source_type=self._memory_source_type(existing),
                ),
            ) if existing else None
            if proposal and proposal.action in {ResolutionAction.MERGE_PROVENANCE, ResolutionAction.REINFORCE}:
                self.store.add_memory_source(existing.id, event.id)
                if proposal.action is ResolutionAction.REINFORCE:
                    self.store.reinforce_memory(existing.id)
                self.store.record_transition(
                    namespace=event.namespace, from_memory_id=existing.id, to_memory_id=existing.id,
                    relationship=proposal.relationship.value, action=proposal.action.value,
                    confidence=proposal.confidence, reason=proposal.reason, source_event_id=event.id,
                    resolver_version=proposal.resolver_version, created_at=utc_now(),
                )
                memory_ids.append(existing.id)
                continue
            if existing and proposal and proposal.action is ResolutionAction.SUPERSEDE:
                self.store.close_validity(existing.id, event.observed_at)
                self.store.update_status(existing.id, MemoryStatus.SUPERSEDED)
                memory.supersedes_id = existing.id
                memory.version = existing.version + 1
            elif existing and proposal and proposal.action is ResolutionAction.COEXIST:
                memory.contradicts_id = existing.id
            self.store.add_memory(memory)
            for entity_name, entity_type in entity_candidates(memory.content, memory.structured_content):
                self.store.attach_entity(event.namespace, memory.id, entity_name, entity_type)
            predicate = relation_predicate(str(memory.structured_content.get("predicate", "")))
            if predicate and is_user_subject(memory.structured_content.get("subject")):
                # Relations hang off the *user* entity, so only first-person
                # facts may create them. A third-party fact still becomes a
                # memory with its own entity, just not a user relation.
                self.store.add_relation(event.namespace, memory.id, f"user:{event.namespace}", predicate,
                                        str(memory.structured_content.get("value", "")), memory.valid_from,
                                        memory.valid_to, memory.confidence, event.id)
                self._refresh_profile(event.namespace)
            self.store.record_transition(
                namespace=event.namespace,
                from_memory_id=(existing.id if existing and proposal and proposal.action is not ResolutionAction.CREATE else None),
                to_memory_id=memory.id,
                relationship=proposal.relationship.value if proposal else "unrelated",
                action=proposal.action.value if proposal else ResolutionAction.CREATE.value,
                confidence=proposal.confidence if proposal else 1.0,
                reason=proposal.reason if proposal else "no related active memory",
                source_event_id=event.id,
                resolver_version=proposal.resolver_version if proposal else self.resolver.version,
                created_at=utc_now(),
            )
            memory_ids.append(memory.id)
        return memory_ids

    @_synchronized
    def retrieve(self, namespace: str, query: str, *, limit: int = 8, as_of: str | None = None,
                 intent: str | None = None, token_budget: int = 1500,
                 observed_as_of: str | None = None,
                 config: RetrievalConfig | None = None) -> RetrievalResult:
        with self._lock:
            query_plan = plan_query(query, limit=limit, token_budget=token_budget)
            limit = query_plan.limit
            intent = intent or query_plan.intent
            as_of = temporal.require_instant(as_of or query_plan.as_of, "as_of")
            observed_as_of = temporal.require_instant(observed_as_of, "observed_as_of")
            config = config or RetrievalConfig()
            include_history = query_plan.temporal_mode in {"historical", "earliest", "all"} or as_of is not None
            trace: dict[str, Any] = {
                "schema_version": "retrieval-trace-v1",
                "request_id": new_id("req"),
                "query_sha256": sha256(query.encode("utf-8")).hexdigest(),
                "config_version": config.version,
                "config_name": config.name,
                "executed_channels": [],
                "channels": {},
                "candidates": [],
                "exclusions": {"temporal": 0, "model_generation": 0, "deletion_status": 0},
                "hard_filters": {"namespace": True, "deletion_status": True},
            }
            rows = self.store.search(namespace, query, limit=limit, as_of=as_of,
                                     include_history=include_history, predicates=query_plan.predicates,
                                     temporal_mode=query_plan.temporal_mode, observed_as_of=observed_as_of,
                                     multi_hop=query_plan.intent == "multi_hop", config=config, trace=trace)
            items = [RetrievalItem(memory, score, channels, memory.source_event_ids) for memory, score, channels in rows]
            with self.store.transaction():
                accessed_at = utc_now()
                for rank, item in enumerate(items, start=1):
                    self.store.record_access(item.memory.id, query, rank, item.score, accessed_at)
            abstain = None if items else "no supported memory found"
            return RetrievalResult(items, {"intent": intent, "temporal_mode": query_plan.temporal_mode,
                                           "as_of": as_of, "limit": query_plan.limit,
                                           "observed_as_of": observed_as_of,
                                           "token_budget": query_plan.token_budget,
                                           "predicates": query_plan.predicates,
                                           "channels": trace["executed_channels"],
                                           "retrieval_config": config.name}, abstain,
                                   degraded=any(item.get("degraded", False)
                                                for item in trace["channels"].values()),
                                   trace=trace)

    @_synchronized
    def retrieve_context(self, namespace: str, query: str, *, limit: int = 8, token_budget: int = 1500,
                         as_of: str | None = None, observed_as_of: str | None = None,
                         token_counter: TokenCounter | None = None) -> dict:
        token_budget = max(0, min(int(token_budget), 100_000))
        result = self.retrieve(namespace, query, limit=limit, as_of=as_of,
                               observed_as_of=observed_as_of, token_budget=token_budget)
        current_only = result.plan["temporal_mode"] not in {"historical", "earliest", "all"}
        packed = pack_context(result.items, token_budget, token_counter=token_counter, current_only=current_only)
        return {"context": packed.text, "items": packed.items, "estimated_tokens": packed.estimated_tokens,
                "omitted": packed.omitted, "plan": {**result.plan, "token_budget": token_budget},
                "abstain_reason": result.abstain_reason, "trace": result.trace,
                "packing_decisions": packed.decisions, "token_counter": packed.token_counter,
                "token_count_degraded": packed.token_count_degraded}

    @_synchronized
    def forget(self, memory_id: str, reason: str = "user_request", *, hard: bool = False) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise KeyError(memory_id)
        if hard:
            self.store.purge_memory(memory_id, reason, utc_now())
        else:
            self.store.tombstone_memory(memory_id, reason, utc_now())
        self._refresh_profile(memory.namespace)

    @_synchronized
    def forget_event(self, event_id: str, reason: str = "user_request", *, hard: bool = False) -> list[str]:
        event = self.get_event(event_id)
        if event is None:
            raise KeyError(event_id)
        namespaces = {memory.namespace for memory in self.store.memories_for_event(event_id)}
        namespaces.add(event.namespace)
        deleted = self.store.tombstone_event(event_id, reason, utc_now(), hard=hard)
        for namespace in namespaces:
            self._refresh_profile(namespace)
        return deleted

    @_synchronized
    def forget_scope(self, namespace: str, *, event_id: str | None = None, topic: str | None = None,
                     before: str | None = None, after: str | None = None, hard: bool = False) -> dict:
        before = temporal.require_instant(before, "before")
        after = temporal.require_instant(after, "after")
        if not any((event_id, topic, before, after)):
            raise ValueError("at least one forget selector is required")
        events = list(self.store.events_for_namespace(namespace))
        if event_id:
            events = [event for event in events if event.id == event_id]
            if not events:
                raise KeyError(event_id)
        before_dt = temporal.parse_instant(before)
        after_dt = temporal.parse_instant(after)
        selected: list[Event] = []
        for event in events:
            observed = temporal.parse_instant(event.observed_at)
            if before_dt is not None and (observed is None or observed >= before_dt):
                continue
            if after_dt is not None and (observed is None or observed <= after_dt):
                continue
            if topic and topic.casefold() not in str(event.payload.get("text", "")).casefold():
                continue
            selected.append(event)
        deleted_memories: list[str] = []
        for event in selected:
            deleted_memories.extend(self.forget_event(event.id, hard=hard))
        return {"namespace": namespace, "deleted_event_ids": [event.id for event in selected],
                "deleted_memory_ids": list(dict.fromkeys(deleted_memories)), "hard": hard}

    @_synchronized
    def set_memory_enabled(self, namespace: str, enabled: bool) -> None:
        self.store.set_memory_enabled(namespace, enabled, utc_now())

    @_synchronized
    def pending_events(self, limit: int = 100) -> list[Event]:
        return self.store.pending_events(max(1, min(int(limit), 1000)))

    @_synchronized
    def mark_event_processed(self, event_id: str) -> None:
        self.store.mark_event_processed(event_id, utc_now())

    @_synchronized
    def process_pending(self, limit: int = 100) -> list[dict]:
        results = []
        events = self.store.claim_pending_events(max(1, min(int(limit), 1000)))
        for event in events:
            try:
                with self.store.transaction():
                    if not self.store.memory_enabled(event.namespace):
                        self.store.mark_event_processed(event.id, utc_now())
                        results.append({"event_id": event.id, "memory_ids": [], "memory_disabled": True})
                        continue
                    memory_ids = self._process_event(event)
                    self.store.mark_event_processed(event.id, utc_now())
                results.append({"event_id": event.id, "memory_ids": memory_ids})
            except Exception as exc:
                failure = self.store.mark_event_failed(event.id, f"{type(exc).__name__}: {exc}")
                results.append({"event_id": event.id, "memory_ids": [], "error": str(exc), **failure})
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
                      offset: int = 0, kind: str | None = None, as_of: str | None = None) -> list[Memory]:
        if status is not None and status not in {item.value for item in MemoryStatus}:
            raise ValueError(f"unknown memory status: {status}")
        as_of = temporal.require_instant(as_of, "as_of")
        memories = self.store.memories_for_namespace(namespace, include_deleted=status == "DELETED")
        if status:
            memories = [memory for memory in memories if memory.status.value == status]
        if kind:
            memories = [memory for memory in memories if memory.kind == kind]
        if as_of:
            memories = [memory for memory in memories
                        if temporal.visible_at(memory.valid_from, memory.valid_to, as_of)]
        bounded_limit = max(1, min(int(limit), 1000))
        return memories[max(0, int(offset)):max(0, int(offset)) + bounded_limit]

    @_synchronized
    def profile(self, namespace: str) -> dict:
        return self.store.get_profile(namespace) or {"_meta": {"schema_version": 1, "generated_from_version": 0}}

    def _refresh_profile(self, namespace: str) -> None:
        # For a single-valued predicate the profile must pick by *provenance*
        # before recency. A derived memory (summary / generated / tool) may
        # legitimately coexist with a user fact — the resolver refuses to
        # supersede across that boundary — but it must not become the projected
        # answer. Ties, and the unknown-provenance case, keep the original
        # last-writer-wins order so v1 behaviour is untouched.
        authoritative: dict[str, tuple[str, Any, int]] = {}
        fallback: dict[str, tuple[str, Any, int]] = {}
        profile: dict = {}
        version = 0
        for memory in self.store.active_memories(namespace):
            # The profile is a *user* profile. A memory whose subject is someone
            # else (``Alice``, a customer record, a colleague) is still a
            # first-class memory but must never be projected under the user's
            # predicates — that is exactly the entity-confusion failure the
            # false-memory suite's ``ec-third-party-residence`` case encodes.
            if not is_user_subject(memory.structured_content.get("subject")):
                version = max(version, memory.version)
                continue
            predicate = memory.structured_content.get("predicate")
            value = memory.structured_content.get("value")
            if not predicate or value is None:
                continue
            source_tier = self._memory_source_type(memory)
            if is_single_valued(predicate):
                bucket = authoritative if source_tier in _AUTHORITATIVE_SOURCES else fallback
                bucket[predicate] = (memory.id, value, memory.version)
            elif predicate not in profile:
                profile[predicate] = value
            elif isinstance(profile[predicate], list):
                if value not in profile[predicate]:
                    profile[predicate].append(value)
            elif profile[predicate] != value:
                profile[predicate] = [profile[predicate], value]
            version = max(version, memory.version)
        for predicate, (_, value, _) in fallback.items():
            profile.setdefault(predicate, value)
        for predicate, (_, value, _) in authoritative.items():
            profile[predicate] = value
        self.store.upsert_profile(namespace, profile, version, utc_now())

    @_synchronized
    def correct_memory(self, memory_id: str, content: str, *, namespace: str | None = None,
                       idempotency_key: str | None = None,
                       observed_at: str | None = None,
                       source_type: str | None = "user_correction") -> dict:
        old = self.get_memory(memory_id)
        if old is None:
            raise KeyError(memory_id)
        # ``namespace`` is the tenancy boundary: a correction may restate the
        # target namespace but must never move a memory into someone else's
        # scope (which would also pollute their profile projection).
        if namespace is not None and namespace != old.namespace:
            raise ValueError("a correction cannot move a memory across namespaces")
        if old.status not in {MemoryStatus.ACTIVE, MemoryStatus.REINFORCED, MemoryStatus.ARCHIVED}:
            raise ValueError(f"memory in {old.status.value} state cannot be corrected")
        return self.ingest(old.namespace, content, explicit=True, event_type="memory_correction",
                           supersedes_memory_id=old.id, idempotency_key=idempotency_key,
                           observed_at=observed_at, source_type=source_type)

    @_synchronized
    def memory_versions(self, memory_id: str) -> list[dict]:
        versions = self.store.memory_version_chain(memory_id)
        if not versions:
            raise KeyError(memory_id)
        return versions

    @_synchronized
    def rebuild_projections(self, namespace: str | None = None, *, dry_run: bool = False) -> dict:
        events = list(self.store.events_for_namespace(namespace)) if namespace else self.store.all_events()
        replayable = [event for event in events if not self.store.is_tombstoned("event", event.id)]
        if dry_run:
            return {"namespace": namespace, "events": len(replayable), "memories": 0, "dry_run": True}
        memory_count = 0
        with self.store.transaction(immediate=True):
            self.store.clear_projections(namespace)
            for event in replayable:
                memory_count += len(self._process_event(event))
                self.store.mark_event_processed(event.id, utc_now())
        return {"namespace": namespace, "events": len(replayable), "memories": memory_count, "dry_run": False}

    @_synchronized
    def reindex(self, namespace: str | None = None, *, indexes: list[str] | None = None,
                dry_run: bool = False) -> dict:
        if indexes is not None and not indexes:
            raise ValueError("at least one index is required")
        requested = list(dict.fromkeys(indexes or ["lexical", "vector"]))
        allowed = {"lexical", "vector", "projections"}
        unknown = set(requested) - allowed
        if unknown:
            raise ValueError(f"unknown indexes: {', '.join(sorted(unknown))}")
        if "projections" in requested and len(requested) != 1:
            raise ValueError("projections must be requested by itself")
        job_id = new_id("job")
        self.store.create_job(job_id, "reindex", namespace, dry_run, utc_now())
        if dry_run:
            result = {"namespace": namespace, "indexes": requested, "counts": {}, "dry_run": True}
            self.store.finish_job(job_id, "DONE", result, utc_now())
            return result
        try:
            if "projections" in requested:
                replay = self.rebuild_projections(namespace)
                result = {"namespace": namespace, "indexes": ["projections"],
                          "counts": {"events": replay["events"], "memories": replay["memories"]},
                          "dry_run": False}
            else:
                counts: dict[str, int] = {}
                if "lexical" in requested:
                    counts["lexical"] = self.store.reindex_lexical(namespace)
                if "vector" in requested:
                    counts["vector"] = self.store.reindex_vectors(namespace)
                result = {"namespace": namespace, "indexes": requested, "counts": counts, "dry_run": False}
        except Exception as exc:
            self.store.finish_job(job_id, "FAILED", {"error": str(exc)}, utc_now())
            raise
        self.store.finish_job(job_id, "DONE", result, utc_now())
        return result

    @_synchronized
    def consolidate(self, namespace: str, *, dry_run: bool = False) -> ConsolidationReport:
        job_id = new_id("job")
        self.store.create_job(job_id, "consolidate", namespace, dry_run, utc_now())
        try:
            report = consolidate(self.store, namespace, dry_run=dry_run)
        except Exception as exc:
            self.store.finish_job(job_id, "FAILED", {"error": str(exc)}, utc_now())
            raise
        self.store.finish_job(job_id, "DONE", {
            "examined": report.examined, "merged": report.merged, "archived": report.archived,
        }, utc_now())
        return report

    @_synchronized
    def archive_expired(self, namespace: str, *, now: str | None = None, dry_run: bool = False) -> int:
        now = temporal.require_instant(now, "now")
        job_id = new_id("job")
        self.store.create_job(job_id, "archive", namespace, dry_run, utc_now())
        try:
            result = archive_expired(self.store, namespace, now=now, dry_run=dry_run)
        except Exception as exc:
            self.store.finish_job(job_id, "FAILED", {"error": str(exc)}, utc_now())
            raise
        if not dry_run:
            self._refresh_profile(namespace)
        self.store.finish_job(job_id, "DONE", {"archived": result}, utc_now())
        return result

    @_synchronized
    def decay(self, namespace: str, *, now: str | None = None, dry_run: bool = False) -> dict:
        now = temporal.require_instant(now, "now")
        job_id = new_id("job")
        self.store.create_job(job_id, "decay", namespace, dry_run, utc_now())
        try:
            result = apply_decay(self.store, namespace, now=now, dry_run=dry_run)
        except Exception as exc:
            self.store.finish_job(job_id, "FAILED", {"error": str(exc)}, utc_now())
            raise
        # A dry run must leave every projection untouched, including profiles.
        if not dry_run:
            self._refresh_profile(namespace)
        self.store.finish_job(job_id, "DONE", result, utc_now())
        return result

    @_synchronized
    def jobs(self, namespace: str | None = None, *, limit: int = 100) -> list[dict]:
        return self.store.list_jobs(namespace, max(1, min(int(limit), 1000)))

    @_synchronized
    def reinforce(self, memory_id: str, *, namespace: str | None = None) -> Memory:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise KeyError(memory_id)
        if namespace is not None and namespace != memory.namespace:
            raise ValueError("reinforcement cannot cross namespaces")
        if memory.status not in {MemoryStatus.ACTIVE, MemoryStatus.REINFORCED, MemoryStatus.ARCHIVED}:
            raise ValueError(f"memory in {memory.status.value} state cannot be reinforced")
        self.store.reinforce_memory(memory_id)
        self._refresh_profile(memory.namespace)
        return self.get_memory(memory_id)  # type: ignore[return-value]

    @_synchronized
    def timeline(self, namespace: str, *, as_of: str | None = None,
                 from_time: str | None = None, to_time: str | None = None) -> list[Memory]:
        as_of = temporal.require_instant(as_of, "as_of")
        from_time = temporal.require_instant(from_time, "from_time")
        to_time = temporal.require_instant(to_time, "to_time")
        if from_time and to_time and temporal.parse_instant(from_time) >= temporal.parse_instant(to_time):
            raise ValueError("from_time must be earlier than to_time")
        return [memory for memory in self.store.memories_for_namespace(namespace)
                if temporal.visible_at(memory.valid_from, memory.valid_to, as_of)
                and temporal.overlaps(memory.valid_from, memory.valid_to, from_time, to_time)]

    @_synchronized
    def export_namespace(self, namespace: str, path: str | None = None) -> dict:
        payload = {
            "namespace": namespace,
            "events": [{
                "id": event.id, "namespace": event.namespace, "event_type": event.event_type, "payload": event.payload,
                "observed_at": event.observed_at, "occurred_from": event.occurred_from,
                "occurred_to": event.occurred_to, "source_message_id": event.source_message_id,
                "idempotency_key": event.idempotency_key, "created_at": event.created_at,
            } for event in self.store.events_for_namespace(namespace)],
            "memories": [{"id": memory.id, "kind": memory.kind, "content": memory.content,
                          "structured_content": memory.structured_content, "status": memory.status.value,
                          "evidence_state": memory.evidence_state.value, "valid_from": memory.valid_from,
                          "valid_to": memory.valid_to, "observed_at": memory.observed_at,
                          "version": memory.version, "supersedes_id": memory.supersedes_id,
                          "source_event_ids": memory.source_event_ids,
                          "versions": self.store.memory_version_chain(memory.id)}
                         for memory in self.store.memories_for_namespace(namespace, include_deleted=True)],
            "profile": self.profile(namespace),
            "relations": [dict(row) for row in self.store.db.execute(
                "SELECT r.id,r.predicate,r.valid_from,r.valid_to,r.confidence,r.source_event_id,"
                "s.canonical_name AS subject,o.canonical_name AS object "
                "FROM relations r JOIN entities s ON s.id=r.subject_entity_id "
                "JOIN entities o ON o.id=r.object_entity_id WHERE r.namespace=? ORDER BY r.id", (namespace,))],
            "transitions": [dict(row) for row in self.store.db.execute(
                "SELECT * FROM memory_transitions WHERE namespace=? ORDER BY created_at,id", (namespace,))],
            "tombstones": [dict(row) for row in self.store.db.execute(
                "SELECT * FROM tombstones WHERE namespace=? ORDER BY deleted_at, object_id", (namespace,))],
        }
        if path:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
