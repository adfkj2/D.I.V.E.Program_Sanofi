from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable

from .ids import cosine, stable_vector
from .models import Event, EvidenceState, Memory, MemoryStatus, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_type TEXT NOT NULL,
  payload TEXT NOT NULL, observed_at TEXT NOT NULL, occurred_from TEXT,
  occurred_to TEXT, source_message_id TEXT, idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING',
  created_at TEXT NOT NULL, processed_at TEXT
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
  evidence_state TEXT NOT NULL, content TEXT NOT NULL, structured_content TEXT NOT NULL,
  status TEXT NOT NULL, importance REAL NOT NULL, confidence REAL NOT NULL,
  salience REAL NOT NULL, durability TEXT NOT NULL, valid_from TEXT,
  valid_to TEXT, observed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  version INTEGER NOT NULL, supersedes_id TEXT, contradicts_id TEXT
);
CREATE TABLE IF NOT EXISTS memory_sources (memory_id TEXT NOT NULL, event_id TEXT NOT NULL,
  PRIMARY KEY(memory_id, event_id));
CREATE TABLE IF NOT EXISTS memory_vectors (memory_id TEXT PRIMARY KEY, vector TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content, structured_content);
CREATE TABLE IF NOT EXISTS tombstones (object_type TEXT NOT NULL, object_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL, deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_access (
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, query TEXT NOT NULL,
  rank INTEGER NOT NULL, score REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
  accessed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
  canonical_name TEXT NOT NULL, entity_type TEXT NOT NULL,
  UNIQUE(namespace, canonical_name)
);
CREATE TABLE IF NOT EXISTS memory_entities (
  memory_id TEXT NOT NULL, entity_id INTEGER NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY(memory_id, entity_id), FOREIGN KEY(entity_id) REFERENCES entities(id)
);
CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
  subject_entity_id INTEGER NOT NULL, predicate TEXT NOT NULL,
  object_entity_id INTEGER NOT NULL, valid_from TEXT, valid_to TEXT,
  confidence REAL NOT NULL, source_event_id TEXT NOT NULL,
  UNIQUE(namespace, subject_entity_id, predicate, object_entity_id, source_event_id)
);
CREATE TABLE IF NOT EXISTS memory_relations (
  memory_id TEXT NOT NULL, relation_id INTEGER NOT NULL,
  PRIMARY KEY(memory_id, relation_id)
);
CREATE TABLE IF NOT EXISTS profiles (
  namespace TEXT PRIMARY KEY, schema_version INTEGER NOT NULL DEFAULT 1,
  current_json TEXT NOT NULL, generated_from_version INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def append_event(self, event: Event) -> bool:
        try:
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event.id, event.namespace, event.event_type, json.dumps(event.payload), event.observed_at,
                 event.occurred_from, event.occurred_to, event.source_message_id, event.idempotency_key, event.created_at),
            )
            self.db.execute("INSERT INTO outbox(event_id, status, created_at) VALUES (?, 'PENDING', ?)",
                            (event.id, event.created_at))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_event(self, event_id: str) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        return Event(row["id"], row["namespace"], row["event_type"], json.loads(row["payload"]), row["observed_at"],
                     row["occurred_from"], row["occurred_to"], row["source_message_id"], row["idempotency_key"], row["created_at"])

    def get_event_by_idempotency(self, key: str) -> Event | None:
        row = self.db.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone()
        return self.get_event(row["id"]) if row else None

    def pending_events(self, limit: int = 100) -> list[Event]:
        rows = self.db.execute("SELECT event_id FROM outbox WHERE status='PENDING' ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [event for row in rows if (event := self.get_event(row["event_id"])) is not None]

    def mark_event_processed(self, event_id: str, processed_at: str) -> None:
        self.db.execute("UPDATE outbox SET status='DONE', processed_at=? WHERE event_id=?", (processed_at, event_id))
        self.db.commit()

    def _memory(self, row: sqlite3.Row) -> Memory:
        sources = [r[0] for r in self.db.execute("SELECT event_id FROM memory_sources WHERE memory_id=?", (row["id"],))]
        return Memory(row["id"], row["namespace"], row["kind"], EvidenceState(row["evidence_state"]), row["content"],
                      json.loads(row["structured_content"]), MemoryStatus(row["status"]), row["importance"], row["confidence"],
                      row["salience"], row["durability"], row["valid_from"], row["valid_to"], row["observed_at"],
                      row["created_at"], row["updated_at"], row["version"], sources, row["supersedes_id"], row["contradicts_id"])

    def get_memory(self, memory_id: str) -> Memory | None:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._memory(row) if row else None

    def add_memory(self, memory: Memory) -> None:
        self.db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (memory.id, memory.namespace, memory.kind, memory.evidence_state.value, memory.content,
                         json.dumps(memory.structured_content), memory.status.value, memory.importance, memory.confidence,
                         memory.salience, memory.durability, memory.valid_from, memory.valid_to, memory.observed_at,
                         memory.created_at, memory.updated_at, memory.version, memory.supersedes_id, memory.contradicts_id))
        for event_id in memory.source_event_ids:
            self.db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?,?)", (memory.id, event_id))
        self.db.execute("INSERT INTO memory_vectors VALUES (?,?)", (memory.id, json.dumps(stable_vector(memory.content))))
        self.db.execute("INSERT INTO memory_fts VALUES (?,?,?)", (memory.id, memory.content, json.dumps(memory.structured_content)))
        self.db.commit()

    def attach_entity(self, namespace: str, memory_id: str, canonical_name: str, entity_type: str,
                      role: str = "mentioned") -> int:
        self.db.execute("INSERT OR IGNORE INTO entities(namespace, canonical_name, entity_type) VALUES (?,?,?)",
                        (namespace, canonical_name, entity_type))
        entity_id = self.db.execute("SELECT id FROM entities WHERE namespace=? AND canonical_name=?",
                                    (namespace, canonical_name)).fetchone()[0]
        self.db.execute("INSERT OR IGNORE INTO memory_entities VALUES (?,?,?)", (memory_id, entity_id, role))
        self.db.commit()
        return int(entity_id)

    def add_relation(self, namespace: str, memory_id: str, subject_name: str, predicate: str,
                     object_name: str, valid_from: str | None, valid_to: str | None,
                     confidence: float, source_event_id: str) -> int:
        subject_id = self.attach_entity(namespace, memory_id, subject_name, "user")
        object_id = self.attach_entity(namespace, memory_id, object_name, "concept")
        self.db.execute(
            "INSERT OR IGNORE INTO relations(namespace,subject_entity_id,predicate,object_entity_id,valid_from,valid_to,confidence,source_event_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (namespace, subject_id, predicate, object_id, valid_from, valid_to, confidence, source_event_id),
        )
        relation_id = self.db.execute(
            "SELECT id FROM relations WHERE namespace=? AND subject_entity_id=? AND predicate=? AND object_entity_id=? AND source_event_id=?",
            (namespace, subject_id, predicate, object_id, source_event_id),
        ).fetchone()[0]
        self.db.execute("INSERT OR IGNORE INTO memory_relations VALUES (?,?)", (memory_id, relation_id))
        self.db.commit()
        return int(relation_id)

    def update_status(self, memory_id: str, status: MemoryStatus) -> None:
        self.db.execute("UPDATE memories SET status=? WHERE id=?", (status.value, memory_id))
        self.db.commit()

    def update_importance(self, memory_id: str, importance: float) -> None:
        self.db.execute("UPDATE memories SET importance=?, updated_at=? WHERE id=?", (importance, utc_now(), memory_id))
        self.db.commit()

    def upsert_profile(self, namespace: str, current: dict, generated_from_version: int, updated_at: str) -> None:
        self.db.execute(
            "INSERT INTO profiles(namespace,schema_version,current_json,generated_from_version,updated_at) VALUES (?,?,?, ?,?) "
            "ON CONFLICT(namespace) DO UPDATE SET current_json=excluded.current_json, generated_from_version=excluded.generated_from_version, updated_at=excluded.updated_at",
            (namespace, 1, json.dumps(current, ensure_ascii=False), generated_from_version, updated_at),
        )
        self.db.commit()

    def get_profile(self, namespace: str) -> dict | None:
        row = self.db.execute("SELECT * FROM profiles WHERE namespace=?", (namespace,)).fetchone()
        if not row:
            return None
        value = json.loads(row["current_json"])
        value["_meta"] = {"schema_version": row["schema_version"], "generated_from_version": row["generated_from_version"], "updated_at": row["updated_at"]}
        return value

    def close_validity(self, memory_id: str, valid_to: str) -> None:
        self.db.execute("UPDATE memories SET valid_to=?, updated_at=? WHERE id=?", (valid_to, valid_to, memory_id))
        self.db.commit()

    def active_memories(self, namespace: str) -> list[Memory]:
        rows = self.db.execute("SELECT * FROM memories WHERE namespace=? AND status='ACTIVE'", (namespace,)).fetchall()
        return [self._memory(row) for row in rows]

    def search(self, namespace: str, query: str, limit: int = 20, as_of: str | None = None,
               include_history: bool = False) -> list[tuple[Memory, float, list[str]]]:
        query_vec = stable_vector(query)
        lexical: dict[str, float] = {}
        terms = [t for t in query.lower().split() if t]
        if terms:
            match_query = " OR ".join('"' + t.replace('"', '') + '"' for t in terms)
            try:
                for row in self.db.execute("SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ?", (match_query,)):
                    lexical[row["memory_id"]] = 1.0 / (1.0 + max(0.0, float(row["rank"])))
            except sqlite3.OperationalError:
                pass
        status_clause = "m.status='ACTIVE'" if not include_history else "m.status != 'DELETED'"
        rows = self.db.execute("SELECT m.*, v.vector FROM memories m JOIN memory_vectors v ON v.memory_id=m.id WHERE m.namespace=? AND " + status_clause, (namespace,)).fetchall()
        entity_matches = {row["memory_id"] for row in self.db.execute(
            "SELECT me.memory_id FROM memory_entities me JOIN entities e ON e.id=me.entity_id "
            "WHERE e.namespace=? AND instr(?, e.canonical_name) > 0", (namespace, query.lower()))}
        relation_matches = {row["memory_id"] for row in self.db.execute(
            "SELECT mr.memory_id FROM memory_relations mr JOIN relations r ON r.id=mr.relation_id "
            "JOIN entities s ON s.id=r.subject_entity_id JOIN entities o ON o.id=r.object_entity_id "
            "WHERE r.namespace=? AND (instr(?, s.canonical_name)>0 OR instr(?, o.canonical_name)>0 OR instr(?, r.predicate)>0)",
            (namespace, query.lower(), query.lower(), query.lower()))}
        scored: list[tuple[Memory, float, list[str]]] = []
        for row in rows:
            memory = self._memory(row)
            if as_of and memory.valid_from and memory.valid_from > as_of:
                continue
            if as_of and memory.valid_to and memory.valid_to <= as_of:
                continue
            dense = cosine(query_vec, json.loads(row["vector"]))
            sparse = lexical.get(memory.id, 0.0)
            entity_boost = 0.2 if memory.id in entity_matches else 0.0
            relation_boost = 0.3 if memory.id in relation_matches else 0.0
            score = 0.55 * dense + 0.25 * sparse + entity_boost + relation_boost
            channels = (["dense"] if dense > 0 else []) + (["bm25"] if sparse else [])
            if memory.id in entity_matches:
                channels.append("entity")
            if memory.id in relation_matches:
                channels.append("relation")
            # A weak hash-vector collision without lexical or semantic support
            # must not turn into a false memory. Production embedders should
            # calibrate this threshold on the evaluation harness.
            if not channels or score < 0.15:
                continue
            scored.append((memory, score, channels))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def tombstone_memory(self, memory_id: str, reason: str, deleted_at: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO tombstones VALUES ('memory',?,?,?)", (memory_id, reason, deleted_at))
        self.db.execute("UPDATE memories SET status='DELETED' WHERE id=?", (memory_id,))
        self.db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        self.db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (memory_id,))
        self.db.commit()

    def record_access(self, memory_id: str, query: str, rank: int, score: float, accessed_at: str, used: bool = False) -> None:
        self.db.execute("INSERT INTO memory_access(memory_id,query,rank,score,used,accessed_at) VALUES (?,?,?,?,?,?)",
                        (memory_id, query, rank, score, int(used), accessed_at))
        self.db.commit()

    def memories_for_event(self, event_id: str) -> list[Memory]:
        rows = self.db.execute("SELECT m.* FROM memories m JOIN memory_sources s ON s.memory_id=m.id WHERE s.event_id=?", (event_id,)).fetchall()
        return [self._memory(row) for row in rows]

    def tombstone_event(self, event_id: str, reason: str, deleted_at: str, *, hard: bool = False) -> list[str]:
        affected = self.memories_for_event(event_id)
        deleted_ids: list[str] = []
        for memory in affected:
            source_count = self.db.execute("SELECT COUNT(*) FROM memory_sources WHERE memory_id=?", (memory.id,)).fetchone()[0]
            if source_count <= 1:
                self.tombstone_memory(memory.id, reason, deleted_at)
                deleted_ids.append(memory.id)
            else:
                self.db.execute("DELETE FROM memory_sources WHERE memory_id=? AND event_id=?", (memory.id, event_id))
        self.db.execute("INSERT OR REPLACE INTO tombstones VALUES ('event',?,?,?)", (event_id, reason, deleted_at))
        # A deleted deferred event must never be replayed by the outbox worker.
        self.db.execute("UPDATE outbox SET status='DONE', processed_at=? WHERE event_id=? AND status='PENDING'",
                        (deleted_at, event_id))
        # Preserve source events by default so provenance and audit remain
        # traversable. Hard purge is a separate explicit retention workflow.
        if hard:
            self.db.execute("DELETE FROM memory_sources WHERE event_id=?", (event_id,))
            self.db.execute("DELETE FROM events WHERE id=?", (event_id,))
        self.db.commit()
        return deleted_ids

    def events_for_namespace(self, namespace: str) -> Iterable[Event]:
        for row in self.db.execute("SELECT * FROM events WHERE namespace=? ORDER BY observed_at", (namespace,)):
            yield self.get_event(row["id"])  # type: ignore[misc]

    def memories_for_namespace(self, namespace: str, *, include_deleted: bool = False) -> list[Memory]:
        clause = "" if include_deleted else " AND status != 'DELETED'"
        rows = self.db.execute("SELECT * FROM memories WHERE namespace=?" + clause + " ORDER BY observed_at", (namespace,)).fetchall()
        return [self._memory(row) for row in rows]
