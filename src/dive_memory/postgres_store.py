from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

from . import temporal
from .embeddings import DeterministicEmbeddingProvider, EmbeddingProvider, embed_with_metadata
from .ids import stable_id
from .models import Event, EvidenceState, Memory, MemoryStatus, utc_now
from .normalization import normalize_text
from .retrieval import RetrievalConfig


class PostgresStore:
    """PostgreSQL/pgvector adapter for the online ingest/retrieve/delete path.

    Optional dependencies are imported lazily so default SQLite CI remains
    dependency-free. Migrations must be applied before constructing the store.
    """

    def __init__(self, dsn: str, embedder: EmbeddingProvider | None = None) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from pgvector.psycopg import register_vector
        except ImportError as exc:  # pragma: no cover - optional environment
            raise RuntimeError("install dive-memory[postgres] to use PostgresStore") from exc
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        register_vector(self.connection)
        self.embedder = embedder or DeterministicEmbeddingProvider()
        self._transaction_depth = 0
        self._ensure_embedding_generation()

    @staticmethod
    def embedding_generation_id(provider: EmbeddingProvider) -> str:
        return stable_id(
            "embgen", str(getattr(provider, "provider_name", type(provider).__name__)),
            provider.model, str(getattr(provider, "revision", "unversioned")), str(provider.dimensions),
        )

    def _ensure_embedding_generation(self) -> None:
        generation = self.embedding_generation_id(self.embedder)
        provider = str(getattr(self.embedder, "provider_name", type(self.embedder).__name__))
        revision = str(getattr(self.embedder, "revision", "unversioned"))
        self.connection.execute(
            "INSERT INTO embedding_generations(id,provider,model,revision,dimensions,status,activated_at) "
            "VALUES (%s,%s,%s,%s,%s,'ACTIVE',now()) ON CONFLICT(id) DO NOTHING",
            (generation, provider, self.embedder.model, revision, self.embedder.dimensions),
        )
        row = self.connection.execute("SELECT active_generation FROM embedding_state WHERE id=true").fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO embedding_state(id,active_generation) VALUES (true,%s)", (generation,)
            )
        elif row["active_generation"] != generation:
            raise RuntimeError("embedding generation differs from active PostgreSQL generation")

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        del immediate
        if self._transaction_depth:
            self._transaction_depth += 1
            try:
                yield self
            finally:
                self._transaction_depth -= 1
            return
        with self.connection.transaction():
            self._transaction_depth = 1
            try:
                yield self
            finally:
                self._transaction_depth = 0

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _iso(value: Any) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else (str(value) if value is not None else None)

    def _memory(self, row: dict[str, Any]) -> Memory:
        sources = [item["event_id"] for item in self.connection.execute(
            "SELECT event_id FROM memory_sources WHERE memory_id=%s ORDER BY event_id", (row["id"],)
        )]
        valid = row.get("valid_window")
        valid_from = self._iso(valid.lower) if valid is not None else None
        valid_to = self._iso(valid.upper) if valid is not None else None
        return Memory(
            row["id"], row["namespace"], row["kind"], EvidenceState(row["evidence_state"]), row["content"],
            dict(row["structured_content"]), MemoryStatus(row["status"]), float(row["importance"]),
            float(row["confidence"]), float(row["salience"]), row["durability"], valid_from, valid_to,
            self._iso(row.get("observed_at")), self._iso(row["created_at"]) or utc_now(),
            self._iso(row["updated_at"]) or utc_now(), int(row["version"]), sources,
            row.get("supersedes_id"), row.get("contradicts_id"), row.get("model_version"),
            row.get("extractor_version"),
        )

    def append_event(self, event: Event) -> bool:
        from psycopg.types.json import Jsonb

        with self.transaction():
            row = self.connection.execute(
                "INSERT INTO events(id,namespace,event_type,payload,observed_at,occurred_from,occurred_to,"
                "source_message_id,idempotency_key,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (event.id, event.namespace, event.event_type, Jsonb(event.payload), event.observed_at,
                 event.occurred_from, event.occurred_to, event.source_message_id, event.idempotency_key,
                 event.created_at),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute("INSERT INTO outbox(event_id) VALUES (%s)", (event.id,))
            return True

    def get_event(self, event_id: str) -> Event | None:
        row = self.connection.execute("SELECT * FROM events WHERE id=%s", (event_id,)).fetchone()
        if row is None:
            return None
        return Event(
            row["id"], row["namespace"], row["event_type"], dict(row["payload"]),
            self._iso(row["observed_at"]) or "", self._iso(row["occurred_from"]),
            self._iso(row["occurred_to"]), row["source_message_id"], row["idempotency_key"],
            self._iso(row["created_at"]) or utc_now(),
        )

    def get_event_by_idempotency(self, namespace: str, key: str | None) -> Event | None:
        if key is None:
            return None
        row = self.connection.execute(
            "SELECT id FROM events WHERE namespace=%s AND idempotency_key=%s", (namespace, key)
        ).fetchone()
        return self.get_event(row["id"]) if row else None

    def mark_event_processed(self, event_id: str, processed_at: str) -> None:
        self.connection.execute(
            "UPDATE outbox SET status='DONE',processed_at=%s,claimed_at=NULL,last_error=NULL WHERE event_id=%s",
            (processed_at, event_id),
        )

    def mark_event_failed(self, event_id: str, error: str) -> dict[str, int | str | bool]:
        row = self.connection.execute(
            "UPDATE outbox SET status='FAILED',attempts=attempts+1,last_error=%s,claimed_at=NULL "
            "WHERE event_id=%s RETURNING attempts,status", (error[:2000], event_id),
        ).fetchone()
        attempts = int(row["attempts"]) if row else 0
        return {"attempts": attempts, "status": row["status"] if row else "MISSING",
                "retryable": bool(row and attempts < 3)}

    def memory_enabled(self, namespace: str) -> bool:
        row = self.connection.execute(
            "SELECT enabled FROM memory_modes WHERE namespace=%s", (namespace,)
        ).fetchone()
        return True if row is None else bool(row["enabled"])

    def set_memory_enabled(self, namespace: str, enabled: bool, updated_at: str) -> None:
        self.connection.execute(
            "INSERT INTO memory_modes(namespace,enabled,updated_at) VALUES (%s,%s,%s) "
            "ON CONFLICT(namespace) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
            (namespace, enabled, updated_at),
        )

    def record_write_decision(self, event_id: str, candidate_index: int, *, accepted: bool,
                              importance: float, confidence: float, salience: float,
                              durability: str, reason: str, extractor_version: str,
                              processed_at: str, outcome_code: str = "COMMITTED",
                              features: dict | None = None,
                              policy_version: str = "utility-baseline-v1",
                              prompt_version: str | None = None,
                              model_version: str | None = None,
                              schema_version: str | None = None) -> None:
        from psycopg.types.json import Jsonb

        self.connection.execute(
            "INSERT INTO write_decisions(event_id,candidate_index,accepted,importance,confidence,salience,durability,"
            "reason,extractor_version,processed_at,outcome_code,features_json,policy_version,prompt_version,"
            "model_version,schema_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(event_id,candidate_index) DO UPDATE SET accepted=excluded.accepted,reason=excluded.reason,"
            "outcome_code=excluded.outcome_code,features_json=excluded.features_json",
            (event_id, candidate_index, accepted, importance, confidence, salience, durability, reason,
             extractor_version, processed_at, outcome_code, Jsonb(features or {}), policy_version,
             prompt_version, model_version, schema_version),
        )

    def is_tombstoned(self, object_type: str, object_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM tombstones WHERE object_type=%s AND object_id=%s", (object_type, object_id)
        ).fetchone() is not None

    def add_memory(self, memory: Memory) -> None:
        from pgvector import Vector
        from psycopg.types.json import Jsonb

        embedding = embed_with_metadata(self.embedder, memory.content)
        generation = self.embedding_generation_id(self.embedder)
        self.connection.execute(
            "INSERT INTO memories(id,namespace,kind,evidence_state,content,structured_content,status,importance,"
            "confidence,salience,durability,valid_window,observed_at,created_at,updated_at,version,supersedes_id,"
            "contradicts_id,model_version,extractor_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "tstzrange(%s,%s,'[)'),%s,%s,%s,%s,%s,%s,%s,%s)",
            (memory.id, memory.namespace, memory.kind, memory.evidence_state.value, memory.content,
             Jsonb(memory.structured_content), memory.status.value, memory.importance, memory.confidence,
             memory.salience, memory.durability, memory.valid_from, memory.valid_to, memory.observed_at,
             memory.created_at, memory.updated_at, memory.version, memory.supersedes_id, memory.contradicts_id,
             memory.model_version, memory.extractor_version),
        )
        for event_id in memory.source_event_ids:
            self.connection.execute(
                "INSERT INTO memory_sources(memory_id,event_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (memory.id, event_id),
            )
        structured = memory.structured_content
        subject = normalize_text(str(structured.get("subject", "user")), casefold=True)
        predicate = normalize_text(str(structured.get("predicate", "statement")), casefold=True)
        value = normalize_text(str(structured.get("normalized_value", structured.get("value", memory.content))),
                               casefold=True)
        self.connection.execute(
            "INSERT INTO memory_keys(memory_id,namespace,subject_key,predicate_key,value_key) VALUES (%s,%s,%s,%s,%s)",
            (memory.id, memory.namespace, subject, predicate, value),
        )
        self.connection.execute(
            "INSERT INTO memory_vectors(memory_id,generation_id,embedding,provider,model,revision,dimensions,degraded) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (memory.id, generation, Vector(list(embedding.vector)), embedding.provider, embedding.model,
             embedding.revision, embedding.dimensions, embedding.degraded),
        )
        snapshot = {
            "namespace": memory.namespace, "kind": memory.kind, "evidence_state": memory.evidence_state.value,
            "content": memory.content, "structured_content": memory.structured_content,
            "status": memory.status.value, "importance": memory.importance, "confidence": memory.confidence,
            "valid_from": memory.valid_from, "valid_to": memory.valid_to,
            "supersedes_id": memory.supersedes_id,
        }
        self.connection.execute(
            "INSERT INTO memory_versions(memory_id,version,snapshot,reason,source_event_id,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (memory.id, memory.version, Jsonb(snapshot), "supersession" if memory.supersedes_id else "created",
             memory.source_event_ids[0] if memory.source_event_ids else None, memory.created_at),
        )

    def add_memory_source(self, memory_id: str, event_id: str) -> None:
        self.connection.execute(
            "INSERT INTO memory_sources(memory_id,event_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (memory_id, event_id),
        )

    def get_memory(self, memory_id: str) -> Memory | None:
        row = self.connection.execute("SELECT * FROM memories WHERE id=%s", (memory_id,)).fetchone()
        return self._memory(row) if row else None

    def memories_for_event(self, event_id: str) -> list[Memory]:
        rows = self.connection.execute(
            "SELECT m.* FROM memories m JOIN memory_sources s ON s.memory_id=m.id WHERE s.event_id=%s",
            (event_id,),
        ).fetchall()
        return [self._memory(row) for row in rows]

    def memories_for_namespace(self, namespace: str, *, include_deleted: bool = False) -> list[Memory]:
        clause = "" if include_deleted else " AND status!='DELETED'"
        rows = self.connection.execute(
            "SELECT * FROM memories WHERE namespace=%s" + clause + " ORDER BY observed_at,created_at,id",
            (namespace,),
        ).fetchall()
        return [self._memory(row) for row in rows]

    def active_memories(self, namespace: str) -> list[Memory]:
        rows = self.connection.execute(
            "SELECT * FROM memories WHERE namespace=%s AND status IN ('ACTIVE','REINFORCED') "
            "ORDER BY observed_at,created_at,id", (namespace,),
        ).fetchall()
        return [self._memory(row) for row in rows]

    def find_related_memory(self, candidate: Memory, *, allow_different_value: bool) -> Memory | None:
        structured = candidate.structured_content
        subject = normalize_text(str(structured.get("subject", "user")), casefold=True)
        predicate = normalize_text(str(structured.get("predicate", "statement")), casefold=True)
        value = normalize_text(str(structured.get("normalized_value", structured.get("value", candidate.content))),
                               casefold=True)
        row = self.connection.execute(
            "SELECT m.* FROM memory_keys k JOIN memories m ON m.id=k.memory_id WHERE k.namespace=%s "
            "AND k.subject_key=%s AND k.predicate_key=%s AND k.value_key=%s "
            "AND m.status IN ('ACTIVE','REINFORCED') ORDER BY m.observed_at DESC,m.created_at DESC,m.id DESC LIMIT 1",
            (candidate.namespace, subject, predicate, value),
        ).fetchone()
        if row or not allow_different_value:
            return self._memory(row) if row else None
        row = self.connection.execute(
            "SELECT m.* FROM memory_keys k JOIN memories m ON m.id=k.memory_id WHERE k.namespace=%s "
            "AND k.subject_key=%s AND k.predicate_key=%s AND m.status IN ('ACTIVE','REINFORCED') "
            "ORDER BY m.observed_at DESC,m.created_at DESC,m.id DESC LIMIT 1",
            (candidate.namespace, subject, predicate),
        ).fetchone()
        return self._memory(row) if row else None

    def update_status(self, memory_id: str, status: MemoryStatus) -> None:
        self.connection.execute(
            "UPDATE memories SET status=%s,updated_at=now() WHERE id=%s", (status.value, memory_id)
        )

    def close_validity(self, memory_id: str, valid_to: str) -> None:
        self.connection.execute(
            "UPDATE memories SET valid_window=tstzrange(lower(valid_window),%s,'[)'),updated_at=%s WHERE id=%s",
            (valid_to, valid_to, memory_id),
        )
        self.connection.execute(
            "UPDATE relations SET valid_window=tstzrange(lower(valid_window),%s,'[)') WHERE id IN "
            "(SELECT relation_id FROM memory_relations WHERE memory_id=%s)", (valid_to, memory_id),
        )

    def reinforce_memory(self, memory_id: str, *, importance_delta: float = 0.05,
                         confidence_delta: float = 0.02) -> None:
        self.connection.execute(
            "UPDATE memories SET status='REINFORCED',importance=LEAST(1.0,importance+%s),"
            "confidence=LEAST(1.0,confidence+%s),updated_at=now() WHERE id=%s",
            (importance_delta, confidence_delta, memory_id),
        )

    def record_transition(self, *, namespace: str, from_memory_id: str | None,
                          to_memory_id: str | None, relationship: str, action: str,
                          confidence: float, reason: str, source_event_id: str,
                          resolver_version: str, created_at: str) -> str:
        transition_id = stable_id("trn", namespace, from_memory_id or "", to_memory_id or "",
                                  relationship, source_event_id)
        self.connection.execute(
            "INSERT INTO memory_transitions(id,namespace,from_memory_id,to_memory_id,relationship,action,confidence,"
            "reason,source_event_id,resolver_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (transition_id, namespace, from_memory_id, to_memory_id, relationship, action, confidence,
             reason, source_event_id, resolver_version, created_at),
        )
        return transition_id

    def transitions_for_memory(self, memory_id: str) -> list[dict[str, Any]]:
        return list(self.connection.execute(
            "SELECT * FROM memory_transitions WHERE from_memory_id=%s OR to_memory_id=%s ORDER BY created_at,id",
            (memory_id, memory_id),
        ).fetchall())

    def attach_entity(self, namespace: str, memory_id: str, canonical_name: str, entity_type: str,
                      role: str = "mentioned") -> int:
        row = self.connection.execute(
            "INSERT INTO entities(namespace,canonical_name,entity_type) VALUES (%s,%s,%s) "
            "ON CONFLICT(namespace,canonical_name) DO UPDATE SET entity_type=excluded.entity_type RETURNING id",
            (namespace, canonical_name, entity_type),
        ).fetchone()
        entity_id = int(row["id"])
        self.connection.execute(
            "INSERT INTO memory_entities(memory_id,entity_id,role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (memory_id, entity_id, role),
        )
        return entity_id

    def add_relation(self, namespace: str, memory_id: str, subject_name: str, predicate: str,
                     object_name: str, valid_from: str | None, valid_to: str | None,
                     confidence: float, source_event_id: str) -> int:
        subject_id = self.attach_entity(namespace, memory_id, subject_name, "user")
        object_id = self.attach_entity(namespace, memory_id, object_name, "concept")
        row = self.connection.execute(
            "INSERT INTO relations(namespace,subject_entity_id,predicate,object_entity_id,valid_window,confidence,"
            "source_event_id) VALUES (%s,%s,%s,%s,tstzrange(%s,%s,'[)'),%s,%s) "
            "ON CONFLICT(namespace,subject_entity_id,predicate,object_entity_id,source_event_id) "
            "DO UPDATE SET confidence=excluded.confidence RETURNING id",
            (namespace, subject_id, predicate, object_id, valid_from, valid_to, confidence, source_event_id),
        ).fetchone()
        relation_id = int(row["id"])
        self.connection.execute(
            "INSERT INTO memory_relations(memory_id,relation_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (memory_id, relation_id),
        )
        return relation_id

    def upsert_profile(self, namespace: str, current: dict, generated_from_version: int, updated_at: str) -> None:
        from psycopg.types.json import Jsonb

        self.connection.execute(
            "INSERT INTO profiles(namespace,current_json,generated_from_version,updated_at) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT(namespace) DO UPDATE SET current_json=excluded.current_json,"
            "generated_from_version=excluded.generated_from_version,updated_at=excluded.updated_at",
            (namespace, Jsonb(current), generated_from_version, updated_at),
        )

    def get_profile(self, namespace: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM profiles WHERE namespace=%s", (namespace,)).fetchone()
        if row is None:
            return None
        value = dict(row["current_json"])
        value["_meta"] = {"schema_version": row["schema_version"],
                          "generated_from_version": row["generated_from_version"],
                          "updated_at": self._iso(row["updated_at"])}
        return value

    def search(self, namespace: str, query: str, limit: int = 20, as_of: str | None = None,
               include_history: bool = False, predicates: Iterable[str] | None = None,
               temporal_mode: str = "any", observed_as_of: str | None = None,
               multi_hop: bool = False, config: RetrievalConfig | None = None,
               trace: dict | None = None) -> list[tuple[Memory, float, list[str]]]:
        del temporal_mode, multi_hop
        config = config or RetrievalConfig()
        enabled = config.enabled_channels
        trace = trace if trace is not None else {}
        trace.setdefault("channels", {})
        trace.setdefault("exclusions", {"temporal": 0, "model_generation": 0, "deletion_status": 0})
        trace["executed_channels"] = [channel for channel in (
            "bm25", "dense", "predicate", "temporal", "entity", "relation"
        ) if channel in enabled]
        query_embedding = embed_with_metadata(self.embedder, query) if "dense" in enabled else None
        dense_eligible = bool(
            query_embedding and query_embedding.semantic and not query_embedding.degraded
        )
        if enabled == {"dense"} and not dense_eligible:
            trace["channels"]["dense"] = {
                "candidate_count": 0,
                "latency_ms": 0.0,
                "backend": "disabled-nonsemantic",
            }
            trace["candidates"] = []
            return []
        active_generation = self.connection.execute(
            "SELECT active_generation FROM embedding_state WHERE id=true"
        ).fetchone()["active_generation"]
        status = "m.status NOT IN ('DELETED','MERGED')" if include_history else "m.status IN ('ACTIVE','REINFORCED')"
        if dense_eligible:
            from pgvector import Vector

            query_parameter = Vector(list(query_embedding.vector))
            dense_projection = (
                "CASE WHEN NOT v.degraded THEN 1.0 - (v.embedding <=> %s) "
                "ELSE 0.0 END AS dense_score,"
            )
            select_params: tuple[Any, ...] = (
                query_parameter, query, active_generation, namespace,
            )
        else:
            dense_projection = "0.0::double precision AS dense_score,"
            select_params = (query, active_generation, namespace)
        candidate_order = " ORDER BY m.observed_at,m.id"
        if dense_eligible and enabled == {"dense"}:
            candidate_order = " ORDER BY v.embedding <=> %s,m.id LIMIT %s"
            select_params += (query_parameter, config.depth("dense"))
        rows = self.connection.execute(
            "SELECT m.*,v.degraded,v.generation_id," + dense_projection +
            "ts_rank(m.search_document,plainto_tsquery('simple',%s)) AS lexical_score "
            "FROM memories m JOIN memory_vectors v ON v.memory_id=m.id AND v.generation_id=%s "
            f"WHERE m.namespace=%s AND {status}" + candidate_order,
            select_params,
        ).fetchall()
        entity_matches = {row["memory_id"] for row in self.connection.execute(
            "SELECT me.memory_id FROM memory_entities me JOIN entities e ON e.id=me.entity_id "
            "WHERE e.namespace=%s AND position(lower(e.canonical_name) in lower(%s))>0", (namespace, query),
        )} if "entity" in enabled else set()
        relation_matches = {row["memory_id"] for row in self.connection.execute(
            "SELECT mr.memory_id FROM memory_relations mr JOIN relations r ON r.id=mr.relation_id "
            "JOIN entities s ON s.id=r.subject_entity_id JOIN entities o ON o.id=r.object_entity_id "
            "WHERE r.namespace=%s AND (position(lower(s.canonical_name) in lower(%s))>0 OR "
            "position(lower(o.canonical_name) in lower(%s))>0 OR position(lower(r.predicate) in lower(%s))>0)",
            (namespace, query, query, query),
        )} if "relation" in enabled else set()
        predicate_filter = set(predicates or ())
        candidates: list[dict[str, Any]] = []
        for row in rows:
            memory = self._memory(row)
            if "temporal" in enabled and not temporal.visible_at(memory.valid_from, memory.valid_to, as_of):
                trace["exclusions"]["temporal"] += 1
                continue
            if "temporal" in enabled and observed_as_of:
                observed = temporal.parse_instant(memory.observed_at)
                cutoff = temporal.parse_instant(observed_as_of)
                if observed and cutoff and observed > cutoff:
                    trace["exclusions"]["temporal"] += 1
                    continue
            candidates.append({
                "memory": memory, "dense": float(row["dense_score"]) if dense_eligible else 0.0,
                "bm25": float(row["lexical_score"]) if "bm25" in enabled else 0.0,
                "entity": memory.id in entity_matches, "relation": memory.id in relation_matches,
                "predicate": "predicate" in enabled and memory.structured_content.get("predicate") in predicate_filter,
            })
        ranks: dict[str, dict[str, int]] = {}
        for channel in ("dense", "bm25"):
            ordered = sorted((item for item in candidates if channel in enabled and item[channel] > 0),
                             key=lambda item: (-item[channel], item["memory"].id))[:config.depth(channel)]
            ranks[channel] = {item["memory"].id: rank for rank, item in enumerate(ordered, 1)}
        for channel in ("entity", "relation", "predicate"):
            ordered = sorted((item for item in candidates if channel in enabled and item[channel]),
                             key=lambda item: item["memory"].id)[:config.depth(channel)]
            ranks[channel] = {item["memory"].id: rank for rank, item in enumerate(ordered, 1)}
        scored: list[tuple[Memory, float, list[str]]] = []
        for item in candidates:
            memory = item["memory"]
            channels = [channel for channel in ("dense", "bm25", "entity", "relation", "predicate")
                        if memory.id in ranks[channel]]
            if not channels:
                continue
            score = sum(config.weight(channel) / (config.rrf_k + ranks[channel][memory.id])
                        for channel in channels) * (0.8 + 0.1 * memory.importance + 0.1 * memory.confidence)
            scored.append((memory, score, channels))
        scored.sort(key=lambda item: (-item[1], item[0].id))
        for channel in trace["executed_channels"]:
            trace["channels"][channel] = {"candidate_count": len(ranks.get(channel, {})), "latency_ms": 0.0}
        if "dense" in trace["channels"]:
            trace["channels"]["dense"]["backend"] = "postgres-pgvector-exact-cosine"
        final = scored[:limit]
        trace["candidates"] = [{"memory_id": memory.id, "raw_scores": {}, "matches": {}, "ranks": {
            channel: rank_map[memory.id] for channel, rank_map in ranks.items() if memory.id in rank_map
        }, "final_score": score, "selected": True} for memory, score, _channels in final]
        return final

    def record_access(self, memory_id: str, query: str, rank: int, score: float,
                      accessed_at: str, used: bool = False) -> None:
        self.connection.execute(
            "INSERT INTO memory_access(memory_id,query,rank,score,used,accessed_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (memory_id, query, rank, score, used, accessed_at),
        )

    def tombstone_memory(self, memory_id: str, reason: str, deleted_at: str) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise KeyError(memory_id)
        with self.transaction():
            self.connection.execute(
                "INSERT INTO tombstones(object_type,object_id,namespace,reason,deleted_at) "
                "VALUES ('memory',%s,%s,%s,%s) ON CONFLICT(object_type,object_id) DO UPDATE "
                "SET reason=excluded.reason,deleted_at=excluded.deleted_at",
                (memory_id, memory.namespace, reason, deleted_at),
            )
            self.connection.execute("UPDATE memories SET status='DELETED',updated_at=%s WHERE id=%s",
                                    (deleted_at, memory_id))
            self._delete_memory_projections(memory_id)

    def _delete_memory_projections(self, memory_id: str) -> None:
        self.connection.execute("DELETE FROM memory_vectors WHERE memory_id=%s", (memory_id,))
        self.connection.execute("DELETE FROM memory_relations WHERE memory_id=%s", (memory_id,))
        self.connection.execute("DELETE FROM memory_entities WHERE memory_id=%s", (memory_id,))
        self.connection.execute("DELETE FROM relations WHERE id NOT IN (SELECT relation_id FROM memory_relations)")
        self.connection.execute("DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM memory_entities)")

    def purge_memory(self, memory_id: str, reason: str, deleted_at: str) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise KeyError(memory_id)
        with self.transaction():
            self.connection.execute(
                "INSERT INTO tombstones(object_type,object_id,namespace,reason,deleted_at) "
                "VALUES ('memory',%s,%s,%s,%s) ON CONFLICT(object_type,object_id) DO UPDATE "
                "SET reason=excluded.reason,deleted_at=excluded.deleted_at",
                (memory_id, memory.namespace, reason, deleted_at),
            )
            self._delete_memory_projections(memory_id)
            for table in ("memory_access", "memory_sources", "memory_versions", "memory_keys"):
                self.connection.execute(f"DELETE FROM {table} WHERE memory_id=%s", (memory_id,))
            self.connection.execute(
                "DELETE FROM memory_transitions WHERE from_memory_id=%s OR to_memory_id=%s", (memory_id, memory_id)
            )
            self.connection.execute("DELETE FROM memories WHERE id=%s", (memory_id,))

    def purge_namespace(self, namespace: str) -> None:
        """Test/retention helper that removes one namespace without touching others."""

        with self.transaction():
            ids = [row["id"] for row in self.connection.execute(
                "SELECT id FROM memories WHERE namespace=%s", (namespace,)
            )]
            for memory_id in ids:
                self._delete_memory_projections(memory_id)
            for table in ("memory_access", "memory_sources", "memory_versions", "memory_keys"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE memory_id=ANY(%s)", (ids or ["__none__"],)
                )
            self.connection.execute("DELETE FROM memory_transitions WHERE namespace=%s", (namespace,))
            self.connection.execute("DELETE FROM memories WHERE namespace=%s", (namespace,))
            self.connection.execute("DELETE FROM profiles WHERE namespace=%s", (namespace,))
            self.connection.execute("DELETE FROM memory_modes WHERE namespace=%s", (namespace,))
            self.connection.execute("DELETE FROM jobs WHERE namespace=%s", (namespace,))
            self.connection.execute("DELETE FROM tombstones WHERE namespace=%s", (namespace,))
            self.connection.execute(
                "DELETE FROM write_decisions WHERE event_id IN (SELECT id FROM events WHERE namespace=%s)",
                (namespace,),
            )
            self.connection.execute(
                "DELETE FROM outbox WHERE event_id IN (SELECT id FROM events WHERE namespace=%s)", (namespace,)
            )
            self.connection.execute("DELETE FROM events WHERE namespace=%s", (namespace,))
