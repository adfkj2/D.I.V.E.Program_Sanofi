"""Persistent project memory application shared by all delivery interfaces."""

from collections import OrderedDict
from dataclasses import asdict
from sqlite3 import Error as DatabaseError
from threading import RLock
from time import perf_counter
from typing import Any

from ..context import pack_context
from ..ids import new_id
from ..models import Memory, MemoryStatus, utc_now
from ..oracle.evidence import build_evidence_views, render_evidence_block
from ..oracle.reader import OracleReaderRequest, reader_outcome
from ..service import MemoryService
from ..temporal import parse_instant, require_instant, visible_at
from .briefing import build_brief, consult_records
from .readers import Reader
from .records import ProjectExtractionProvider, ProjectRecord


def memory_view(memory: Memory) -> dict[str, Any]:
    row = asdict(memory)
    row["status"] = memory.status.value
    row["evidence_state"] = memory.evidence_state.value
    return row


class MemoryApplication:
    """Single-process local application; core service owns storage transactions.

    One application lock covers use-case read/modify/write sequences. Persisted
    state is in the core store. Answer traces are bounded, ephemeral and cleared
    on project mutations, preventing stale answers from surviving corrections
    or deletion. Multi-worker writes are outside this local delivery contract.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        reader: Reader | None = None,
        service: MemoryService | None = None,
    ):
        self.service = service or MemoryService(
            db_path, extractor=ProjectExtractionProvider(), deterministic_ids=True
        )
        self.reader = reader
        self._lock = RLock()
        self._traces: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @property
    def mode(self) -> str:
        return "live_model" if self.reader else "evidence_only"

    def close(self) -> None:
        self.service.store.close()

    def recover(self) -> list[dict]:
        """Explicit local maintenance; retries bounded core projection outbox work."""
        with self._lock:
            result = self.service.process_pending()
            self._traces.clear()
            return result

    def inspect_event(self, namespace: str, event_id: str) -> dict:
        with self._lock:
            event = self.service.get_event(event_id)
            if event is None or event.namespace != self._namespace(namespace):
                raise KeyError("event not found in this project")
            return {
                "event": asdict(event),
                "write_decisions": self.service.store.write_decisions_for_event(
                    event_id
                ),
            }

    def _invalidate(self, namespace: str) -> None:
        for key in list(self._traces):
            if self._traces[key]["namespace"] == namespace:
                del self._traces[key]

    @staticmethod
    def _namespace(namespace: str) -> str:
        if (
            not isinstance(namespace, str)
            or not namespace.strip()
            or len(namespace) > 120
        ):
            raise ValueError("project must be nonempty and at most 120 characters")
        return namespace.strip()

    def ingest(
        self,
        namespace: str,
        text: str,
        *,
        idempotency_key: str,
        observed_at: str | None = None,
        session: str = "default",
        explicit: bool = False,
    ) -> dict:
        namespace = self._namespace(namespace)
        if observed_at and parse_instant(
            require_instant(observed_at, "observed_at")
        ) > parse_instant(utc_now()):
            raise ValueError(
                "observed_at cannot be in the future; record future plans as proposed"
            )
        if not text.strip() or len(text) > 12000 or not idempotency_key.strip():
            raise ValueError(
                "text and idempotency key are required; text limit is 12000"
            )
        with self._lock:
            duplicate = self.service.store.get_event_by_idempotency(
                namespace, idempotency_key
            )
            if duplicate and (
                duplicate.payload.get("text") != text
                or duplicate.payload.get("explicit") != explicit
                or duplicate.source_message_id != session
                or (
                    observed_at
                    and parse_instant(duplicate.observed_at)
                    != parse_instant(require_instant(observed_at, "observed_at"))
                )
            ):
                raise ValueError("idempotency key already used with different content")
            result = self.service.ingest(
                namespace,
                text,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
                explicit=explicit,
                source_type="user",
                source_message_id=session,
            )
            self._invalidate(namespace)
            return result

    def record(
        self,
        namespace: str,
        record: ProjectRecord,
        *,
        idempotency_key: str,
        observed_at: str | None = None,
        replaces: str | None = None,
    ) -> dict:
        namespace = self._namespace(namespace)
        when = require_instant(observed_at, "observed_at") or utc_now()
        if parse_instant(when) > parse_instant(utc_now()):
            raise ValueError(
                "observed_at cannot be in the future; record future plans as proposed"
            )
        if not idempotency_key.strip():
            raise ValueError("idempotency key required")
        with self._lock:
            duplicate = self.service.store.get_event_by_idempotency(
                namespace, idempotency_key
            )
            if duplicate:
                if (
                    duplicate.payload.get("text") != record.encode()
                    or duplicate.payload.get("supersedes_memory_id") != replaces
                    or (
                        observed_at
                        and parse_instant(duplicate.observed_at) != parse_instant(when)
                    )
                ):
                    raise ValueError(
                        "idempotency key already used with different content"
                    )
                return {
                    "event_id": duplicate.id,
                    "accepted": False,
                    "duplicate": True,
                    "memory_ids": [
                        m.id
                        for m in self.service.store.memories_for_event(duplicate.id)
                    ],
                }
            old = self._owned(namespace, replaces) if replaces else None
            # Replacements are explicit user actions, not an inferred 'latest wins'
            # rule. Proposals cannot silently close a confirmed constraint.
            if old:
                data = old.structured_content
                if (
                    data.get("key") != record.key
                    or data.get("category") != record.category
                ):
                    raise ValueError(
                        "a replacement must keep its record key and category"
                    )
                if data.get("disposition") != record.disposition:
                    raise ValueError("a proposal cannot replace a confirmed record")
                if old.status not in {MemoryStatus.ACTIVE, MemoryStatus.REINFORCED}:
                    raise ValueError("only an active record can be replaced")
                if parse_instant(when) <= parse_instant(
                    old.valid_from or old.observed_at
                ):
                    raise ValueError(
                        "replacement time must be later than the record it replaces"
                    )
            result = self.service.ingest(
                namespace,
                record.encode(),
                explicit=True,
                source_type="user",
                idempotency_key=idempotency_key,
                observed_at=when,
                source_message_id=record.session,
                supersedes_memory_id=old.id if old else None,
            )
            self._invalidate(namespace)
            return result

    def _owned(self, namespace: str, memory_id: str) -> Memory:
        memory = self.service.get_memory(memory_id)
        if (
            memory is None
            or memory.namespace != namespace
            or memory.status == MemoryStatus.DELETED
        ):
            raise KeyError("memory not found in this project")
        return memory

    def snapshot(self, namespace: str, *, as_of: str | None = None) -> dict:
        namespace = self._namespace(namespace)
        cutoff = require_instant(as_of, "as_of")
        with self._lock:
            all_memories = self.service.store.memories_for_namespace(namespace)
            current = [
                m
                for m in all_memories
                if (
                    visible_at(m.valid_from, m.valid_to, cutoff or utc_now())
                    and (
                        not cutoff
                        or not m.observed_at
                        or parse_instant(m.observed_at) <= parse_instant(cutoff)
                    )
                    and (
                        bool(cutoff)
                        or m.status in {MemoryStatus.ACTIVE, MemoryStatus.REINFORCED}
                    )
                )
            ]
            groups: dict[str, list[Memory]] = {}
            for memory in current:
                data = memory.structured_content
                if (
                    data.get("schema") == "project-record-v1"
                    and data.get("disposition") == "confirmed"
                ):
                    groups.setdefault(data["key"], []).append(memory)
            conflicts = [
                key
                for key, values in groups.items()
                if len({m.structured_content["value"] for m in values}) > 1
            ]
            return {
                "namespace": namespace,
                "as_of": cutoff,
                "mode": self.mode,
                "memories": [memory_view(m) for m in current],
                "timeline": [memory_view(m) for m in all_memories],
                "conflicts": conflicts,
                "record_input_mode": "explicit_user_structured_or_heuristic_text",
            }

    def lookup(self, namespace: str, key: str, *, as_of: str | None = None) -> dict:
        state = self.snapshot(namespace, as_of=as_of)
        records = [
            m
            for m in state["memories"]
            if m["structured_content"].get("key") == key
            and m["structured_content"].get("disposition") == "confirmed"
        ]
        values = {m["structured_content"]["value"] for m in records}
        return {
            "status": "ANSWER" if len(values) == 1 else "ABSTAIN",
            "value": next(iter(values)) if len(values) == 1 else None,
            "reason": "confirmed_record"
            if len(values) == 1
            else "conflict"
            if values
            else "missing_record",
            "sources": records,
            "mode": "structured_lookup_not_llm",
        }

    def brief(
        self, namespace: str, *, as_of: str | None = None, since: str | None = None
    ) -> dict:
        with self._lock:
            return build_brief(self.snapshot(namespace, as_of=as_of), since=since)

    def consult(
        self,
        namespace: str,
        question: str,
        *,
        as_of: str | None = None,
        since: str | None = None,
    ) -> dict:
        with self._lock:
            return consult_records(
                self.snapshot(namespace, as_of=as_of), question, since=since
            )

    def source(self, namespace: str, memory_id: str) -> dict:
        with self._lock:
            memory = self._owned(namespace, memory_id)
            events = [self.service.get_event(e) for e in memory.source_event_ids]
            return {
                "memory": memory_view(memory),
                "events": [asdict(e) for e in events if e],
                "write_decisions": [
                    decision
                    for e in events
                    if e
                    for decision in self.service.store.write_decisions_for_event(e.id)
                ],
                "versions": self.service.memory_versions(memory_id),
            }

    def forget(self, namespace: str, memory_id: str) -> dict:
        with self._lock:
            memory = self._owned(namespace, memory_id)
            # Event erasure also prevents projection rebuild from resurrecting it.
            deleted = []
            for event_id in memory.source_event_ids:
                deleted.extend(self.service.forget_event(event_id, hard=True))
            self._invalidate(namespace)
            return {
                "deleted_memory_ids": sorted(set(deleted)),
                "answer_traces_invalidated": True,
            }

    def answer(
        self,
        namespace: str,
        question: str,
        *,
        as_of: str | None = None,
        limit: int = 12,
        token_budget: int = 1500,
    ) -> dict:
        namespace = self._namespace(namespace)
        cutoff = require_instant(as_of, "as_of")
        if (
            not question.strip()
            or len(question) > 4000
            or not 1 <= limit <= 100
            or not 1 <= token_budget <= 12000
        ):
            raise ValueError("invalid question, retrieval limit or evidence budget")
        with self._lock:
            started = perf_counter()
            result = self.service.retrieve(
                namespace,
                question,
                as_of=cutoff,
                observed_as_of=cutoff,
                limit=limit,
                token_budget=token_budget,
            )
            retrieved_ms = (perf_counter() - started) * 1000

            def render(items):
                return render_evidence_block(
                    build_evidence_views(items, question=question, question_date=cutoff)
                )

            packed = pack_context(
                result.items,
                token_budget,
                question=question,
                current_only=not bool(cutoff)
                and result.plan.get("temporal_mode")
                not in {"historical", "earliest", "all"},
                render_for_budget=render,
            )
            packing_ms = (perf_counter() - started) * 1000 - retrieved_ms
            outcome = None
            if self.reader:
                request = OracleReaderRequest(
                    question,
                    cutoff,
                    cutoff,
                    tuple(packed.visible_items),
                    packed.text,
                    token_budget,
                    packed.omitted,
                    self.reader.policy,
                )
                outcome = reader_outcome(self.reader, request).to_dict()
            trace = {
                "trace_id": new_id("trace"),
                "namespace": namespace,
                "question": question,
                "as_of": cutoff,
                "mode": self.mode,
                "reader": outcome,
                "status": outcome["status"] if outcome else "EVIDENCE_ONLY",
                "answer": outcome["hypothesis"] if outcome else None,
                "evidence": [memory_view(i.memory) for i in packed.visible_items],
                "context": packed.text,
                "estimated_tokens": packed.estimated_tokens,
                "token_counter": packed.token_counter,
                "token_budget": token_budget,
                "packing": packed.decisions,
                "retrieval": result.trace,
                "timing_ms": {
                    "retrieve": retrieved_ms,
                    "pack": packing_ms,
                    "total": (perf_counter() - started) * 1000,
                },
                "cache_replay": False,
            }
            self._traces[trace["trace_id"]] = trace
            while len(self._traces) > 100:
                self._traces.popitem(last=False)
            return trace

    def trace(self, namespace: str, trace_id: str) -> dict:
        with self._lock:
            result = self._traces.get(trace_id)
            if not result or result["namespace"] != namespace:
                raise KeyError("trace not found or invalidated by project change")
            return result

    def readiness(self) -> dict:
        with self._lock:
            try:
                self.service.store.db.execute("SELECT 1").fetchone()
                database = "ready"
            except DatabaseError:
                database = "unavailable"
            return {
                "ready": database == "ready",
                "database": database,
                "reader": "configured_not_network_probed"
                if self.reader
                else "not_configured",
                "mode": self.mode,
                "embedding": self.service.store.embedder.model,
            }
