from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import lexical, temporal
from .ids import cosine
from .embeddings import DeterministicEmbeddingProvider, EmbeddingProvider
from .models import Event, EvidenceState, Memory, MemoryStatus, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_type TEXT NOT NULL,
  payload TEXT NOT NULL, observed_at TEXT NOT NULL, occurred_from TEXT,
  occurred_to TEXT, source_message_id TEXT, idempotency_key TEXT,
  created_at TEXT NOT NULL, UNIQUE(namespace, idempotency_key)
);
CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING',
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at TEXT NOT NULL, claimed_at TEXT, processed_at TEXT
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
  evidence_state TEXT NOT NULL, content TEXT NOT NULL, structured_content TEXT NOT NULL,
  status TEXT NOT NULL, importance REAL NOT NULL, confidence REAL NOT NULL,
  salience REAL NOT NULL, durability TEXT NOT NULL, valid_from TEXT,
  valid_to TEXT, observed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  version INTEGER NOT NULL, supersedes_id TEXT, contradicts_id TEXT,
  model_version TEXT, extractor_version TEXT
);
CREATE TABLE IF NOT EXISTS memory_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
  version INTEGER NOT NULL, snapshot TEXT NOT NULL, reason TEXT NOT NULL,
  source_event_id TEXT, created_at TEXT NOT NULL,
  UNIQUE(memory_id, version)
);
CREATE TABLE IF NOT EXISTS write_decisions (
  event_id TEXT NOT NULL, candidate_index INTEGER NOT NULL,
  accepted INTEGER NOT NULL, importance REAL NOT NULL, confidence REAL NOT NULL,
  salience REAL NOT NULL, durability TEXT NOT NULL, reason TEXT NOT NULL,
  extractor_version TEXT NOT NULL, processed_at TEXT NOT NULL,
  PRIMARY KEY(event_id, candidate_index)
);
CREATE TABLE IF NOT EXISTS memory_sources (memory_id TEXT NOT NULL, event_id TEXT NOT NULL,
  PRIMARY KEY(memory_id, event_id));
CREATE TABLE IF NOT EXISTS memory_vectors (memory_id TEXT PRIMARY KEY, vector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vector_metadata (
  id INTEGER PRIMARY KEY CHECK (id = 1), model TEXT NOT NULL, dimensions INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content, structured_content);
CREATE TABLE IF NOT EXISTS tombstones (object_type TEXT NOT NULL, object_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL, deleted_at TEXT NOT NULL, namespace TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS memory_modes (
  namespace TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, job_type TEXT NOT NULL, namespace TEXT,
  status TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0,
  output_summary TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  completed_at TEXT
);
"""


class SQLiteStore:
    def __init__(self, path: str = ":memory:", embedder: EmbeddingProvider | None = None) -> None:
        # FastAPI executes synchronous handlers in a worker pool. The service
        # serializes access, while this flag allows the same local connection
        # to be used by those workers.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._transaction_depth = 0
        self.db.executescript(SCHEMA)
        self._upgrade_schema()
        self.embedder = embedder or DeterministicEmbeddingProvider()
        metadata = self.db.execute("SELECT model, dimensions FROM vector_metadata WHERE id=1").fetchone()
        self._vector_index_compatible = metadata is None or (
            metadata["model"] == self.embedder.model and metadata["dimensions"] == self.embedder.dimensions
        )
        if metadata is None:
            self.db.execute("INSERT INTO vector_metadata(id, model, dimensions) VALUES (1, ?, ?)",
                            (self.embedder.model, self.embedder.dimensions))
        self.db.commit()

    def _upgrade_schema(self) -> None:
        """Add columns introduced after the first local database schema.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` does not upgrade existing
        tables, so file-backed MVP databases need a small additive migration.
        """
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(outbox)")}
        additions = {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT",
            "claimed_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
        memory_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(memories)")}
        for name in ("model_version", "extractor_version"):
            if name not in memory_columns:
                self.db.execute(f"ALTER TABLE memories ADD COLUMN {name} TEXT")
        tombstone_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tombstones)")}
        if "namespace" not in tombstone_columns:
            self.db.execute("ALTER TABLE tombstones ADD COLUMN namespace TEXT")
            # Content that still exists lets us safely recover tenancy for old
            # tombstones. Already-purged legacy rows remain NULL and are never
            # exposed through a namespace export.
            self.db.execute(
                "UPDATE tombstones SET namespace=(SELECT namespace FROM events WHERE events.id=object_id) "
                "WHERE object_type='event' AND namespace IS NULL"
            )
            self.db.execute(
                "UPDATE tombstones SET namespace=(SELECT namespace FROM memories WHERE memories.id=object_id) "
                "WHERE object_type='memory' AND namespace IS NULL"
            )
        event_sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()[0]
        normalized_sql = "".join(event_sql.lower().split())
        if "unique(namespace,idempotency_key)" not in normalized_sql:
            # v0.2 used a global UNIQUE key, which allowed one tenant's retry
            # key to collide with another tenant and reveal the first event id.
            self.db.execute(
                "CREATE TABLE events_scoped ("
                "id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, "
                "observed_at TEXT NOT NULL, occurred_from TEXT, occurred_to TEXT, source_message_id TEXT, "
                "idempotency_key TEXT, created_at TEXT NOT NULL, UNIQUE(namespace,idempotency_key))"
            )
            self.db.execute("INSERT INTO events_scoped SELECT * FROM events")
            self.db.execute("DROP TABLE events")
            self.db.execute("ALTER TABLE events_scoped RENAME TO events")

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        """Commit a group of projection changes atomically.

        Store methods call ``_commit`` for standalone use. Inside this context
        those commits are deferred until the outermost transaction succeeds.
        """
        outermost = self._transaction_depth == 0
        if outermost:
            if self.db.in_transaction:
                raise RuntimeError("cannot start a transaction while another transaction is open")
            self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                self.db.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self.db.commit()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    @property
    def vector_index_compatible(self) -> bool:
        return self._vector_index_compatible

    def append_event(self, event: Event) -> bool:
        try:
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event.id, event.namespace, event.event_type, json.dumps(event.payload), event.observed_at,
                 event.occurred_from, event.occurred_to, event.source_message_id, event.idempotency_key, event.created_at),
            )
            self.db.execute("INSERT INTO outbox(event_id, status, created_at) VALUES (?, 'PENDING', ?)",
                            (event.id, event.created_at))
            self._commit()
            return True
        except sqlite3.IntegrityError:
            # Without the rollback the connection stays inside an aborted
            # transaction, and the next successful commit would silently
            # persist the partially written event/outbox pair.
            if self._transaction_depth:
                raise
            self.db.rollback()
            return False

    def get_event(self, event_id: str) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        return Event(row["id"], row["namespace"], row["event_type"], json.loads(row["payload"]), row["observed_at"],
                     row["occurred_from"], row["occurred_to"], row["source_message_id"], row["idempotency_key"], row["created_at"])

    def get_event_by_idempotency(self, namespace: str, key: str) -> Event | None:
        row = self.db.execute(
            "SELECT id FROM events WHERE namespace=? AND idempotency_key=?", (namespace, key),
        ).fetchone()
        return self.get_event(row["id"]) if row else None

    def pending_events(self, limit: int = 100) -> list[Event]:
        rows = self.db.execute(
            "SELECT event_id FROM outbox WHERE status IN ('PENDING','FAILED') AND attempts < 3 "
            "ORDER BY created_at, event_id LIMIT ?", (limit,)).fetchall()
        return [event for row in rows if (event := self.get_event(row["event_id"])) is not None]

    def claim_pending_events(self, limit: int = 100, *, max_attempts: int = 3,
                             stale_after_seconds: int = 300) -> list[Event]:
        """Atomically claim a worker batch.

        ``BEGIN IMMEDIATE`` serializes claimers across SQLite connections. A
        crashed worker's old PROCESSING rows are returned to FAILED so they can
        be retried instead of remaining stuck forever.
        """
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(seconds=max(1, stale_after_seconds))).isoformat()
        claimed_at = now.isoformat()
        with self.transaction(immediate=True):
            # An outbox row cannot be processed after its event was removed.
            # Removing it here also keeps a corrupt legacy database from
            # repeatedly scanning the same orphan.
            self.db.execute("DELETE FROM outbox WHERE event_id NOT IN (SELECT id FROM events)")
            self.db.execute(
                "UPDATE outbox SET status='FAILED', attempts=attempts+1, "
                "last_error=COALESCE(last_error,'stale worker claim'), "
                "claimed_at=NULL WHERE status='PROCESSING' AND claimed_at < ?",
                (stale_before,),
            )
            rows = self.db.execute(
                "SELECT event_id FROM outbox WHERE status IN ('PENDING','FAILED') AND attempts < ? "
                "ORDER BY created_at, event_id LIMIT ?", (max_attempts, limit),
            ).fetchall()
            event_ids = [row["event_id"] for row in rows]
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                self.db.execute(
                    f"UPDATE outbox SET status='PROCESSING', claimed_at=?, last_error=NULL "
                    f"WHERE event_id IN ({placeholders})",
                    (claimed_at, *event_ids),
                )
        return [event for event_id in event_ids if (event := self.get_event(event_id)) is not None]

    def mark_event_processed(self, event_id: str, processed_at: str) -> None:
        self.db.execute(
            "UPDATE outbox SET status='DONE', processed_at=?, claimed_at=NULL, last_error=NULL WHERE event_id=?",
            (processed_at, event_id),
        )
        self._commit()

    def mark_event_failed(self, event_id: str, error: str) -> dict[str, int | str | bool]:
        self.db.execute(
            "UPDATE outbox SET status='FAILED', attempts=attempts+1, last_error=?, claimed_at=NULL "
            "WHERE event_id=?", (error[:2000], event_id),
        )
        self._commit()
        row = self.db.execute("SELECT attempts, status FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        attempts = int(row["attempts"]) if row else 0
        return {"attempts": attempts, "status": str(row["status"]) if row else "MISSING",
                "retryable": bool(row and attempts < 3)}

    def _memory(self, row: sqlite3.Row) -> Memory:
        sources = [r[0] for r in self.db.execute("SELECT event_id FROM memory_sources WHERE memory_id=?", (row["id"],))]
        return Memory(row["id"], row["namespace"], row["kind"], EvidenceState(row["evidence_state"]), row["content"],
                      json.loads(row["structured_content"]), MemoryStatus(row["status"]), row["importance"], row["confidence"],
                      row["salience"], row["durability"], row["valid_from"], row["valid_to"], row["observed_at"],
                      row["created_at"], row["updated_at"], row["version"], sources, row["supersedes_id"],
                      row["contradicts_id"], row["model_version"], row["extractor_version"])

    def get_memory(self, memory_id: str) -> Memory | None:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._memory(row) if row else None

    def is_tombstoned(self, object_type: str, object_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM tombstones WHERE object_type=? AND object_id=?", (object_type, object_id),
        ).fetchone() is not None

    def add_memory(self, memory: Memory) -> None:
        if not self._vector_index_compatible:
            raise RuntimeError("embedding model changed; call reindex_vectors before writing")
        # Compute before mutating SQLite. A provider failure must not leave an
        # open transaction containing a memory without its vector/FTS rows.
        vector = self.embedder.embed(memory.content)
        if len(vector) != self.embedder.dimensions:
            raise ValueError(
                f"embedding dimensions do not match provider: expected {self.embedder.dimensions}, got {len(vector)}"
            )
        self.db.execute(
            "INSERT INTO memories(id,namespace,kind,evidence_state,content,structured_content,status,importance,"
            "confidence,salience,durability,valid_from,valid_to,observed_at,created_at,updated_at,version,"
            "supersedes_id,contradicts_id,model_version,extractor_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (memory.id, memory.namespace, memory.kind, memory.evidence_state.value, memory.content,
                         json.dumps(memory.structured_content), memory.status.value, memory.importance, memory.confidence,
                         memory.salience, memory.durability, memory.valid_from, memory.valid_to, memory.observed_at,
                         memory.created_at, memory.updated_at, memory.version, memory.supersedes_id, memory.contradicts_id,
                         memory.model_version, memory.extractor_version))
        for event_id in memory.source_event_ids:
            self.db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?,?)", (memory.id, event_id))
        self.db.execute("INSERT INTO memory_vectors VALUES (?,?)",
                        (memory.id, json.dumps(vector)))
        self.db.execute("INSERT INTO memory_fts VALUES (?,?,?)",
                        (memory.id, lexical.index_document(memory.content),
                         lexical.structured_document(memory.structured_content)))
        snapshot = {
            "namespace": memory.namespace,
            "kind": memory.kind,
            "evidence_state": memory.evidence_state.value,
            "content": memory.content,
            "structured_content": memory.structured_content,
            "status": memory.status.value,
            "importance": memory.importance,
            "confidence": memory.confidence,
            "salience": memory.salience,
            "durability": memory.durability,
            "valid_from": memory.valid_from,
            "valid_to": memory.valid_to,
            "observed_at": memory.observed_at,
            "supersedes_id": memory.supersedes_id,
            "model_version": memory.model_version,
            "extractor_version": memory.extractor_version,
        }
        self.db.execute(
            "INSERT OR REPLACE INTO memory_versions(memory_id,version,snapshot,reason,source_event_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (memory.id, memory.version, json.dumps(snapshot, ensure_ascii=False),
             "supersession" if memory.supersedes_id else "created",
             memory.source_event_ids[0] if memory.source_event_ids else None, memory.created_at),
        )
        self._commit()

    def memory_versions(self, memory_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT version,snapshot,reason,source_event_id,created_at FROM memory_versions "
            "WHERE memory_id=? ORDER BY version", (memory_id,),
        ).fetchall()
        return [{"version": row["version"], "snapshot": json.loads(row["snapshot"]),
                 "reason": row["reason"], "source_event_id": row["source_event_id"],
                 "created_at": row["created_at"]} for row in rows]

    def memory_version_chain(self, memory_id: str) -> list[dict]:
        """Return the complete linear supersession chain containing a memory."""
        memory = self.get_memory(memory_id)
        if memory is None:
            return self.memory_versions(memory_id)
        root = memory
        seen = {root.id}
        while root.supersedes_id:
            parent = self.get_memory(root.supersedes_id)
            if parent is None or parent.id in seen:
                break
            root = parent
            seen.add(root.id)
        chain = [root]
        current = root
        seen = {root.id}
        while True:
            row = self.db.execute(
                "SELECT * FROM memories WHERE supersedes_id=? ORDER BY version, created_at, id LIMIT 1",
                (current.id,),
            ).fetchone()
            if row is None or row["id"] in seen:
                break
            current = self._memory(row)
            chain.append(current)
            seen.add(current.id)
        versions: list[dict] = []
        for item in chain:
            for version in self.memory_versions(item.id):
                versions.append({**version, "memory_id": item.id})
        return sorted(versions, key=lambda item: (item["version"], item["created_at"], item["memory_id"]))

    def record_write_decision(self, event_id: str, candidate_index: int, *, accepted: bool,
                              importance: float, confidence: float, salience: float,
                              durability: str, reason: str, extractor_version: str,
                              processed_at: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO write_decisions(event_id,candidate_index,accepted,importance,confidence,"
            "salience,durability,reason,extractor_version,processed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, candidate_index, int(accepted), importance, confidence, salience,
             durability, reason, extractor_version, processed_at),
        )
        self._commit()

    def set_memory_enabled(self, namespace: str, enabled: bool, updated_at: str) -> None:
        self.db.execute(
            "INSERT INTO memory_modes(namespace,enabled,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(namespace) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            (namespace, int(enabled), updated_at),
        )
        self._commit()

    def memory_enabled(self, namespace: str) -> bool:
        row = self.db.execute("SELECT enabled FROM memory_modes WHERE namespace=?", (namespace,)).fetchone()
        return True if row is None else bool(row["enabled"])

    def create_job(self, job_id: str, job_type: str, namespace: str | None,
                   dry_run: bool, created_at: str) -> None:
        self.db.execute(
            "INSERT INTO jobs(id,job_type,namespace,status,dry_run,created_at) VALUES (?,?,?,?,?,?)",
            (job_id, job_type, namespace, "RUNNING", int(dry_run), created_at),
        )
        self._commit()

    def finish_job(self, job_id: str, status: str, summary: dict, completed_at: str) -> None:
        self.db.execute(
            "UPDATE jobs SET status=?, output_summary=?, completed_at=? WHERE id=?",
            (status, json.dumps(summary, ensure_ascii=False), completed_at, job_id),
        )
        self._commit()

    def list_jobs(self, namespace: str | None = None, limit: int = 100) -> list[dict]:
        if namespace is None:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE namespace=? ORDER BY created_at DESC LIMIT ?", (namespace, limit),
            ).fetchall()
        return [{**dict(row), "dry_run": bool(row["dry_run"]),
                 "output_summary": json.loads(row["output_summary"])} for row in rows]

    def clear_projections(self, namespace: str | None = None) -> None:
        """Delete rebuildable state while preserving events and tombstones."""
        with self.transaction():
            if namespace is None:
                self.db.execute("DELETE FROM memory_fts")
                for table in ("memory_access", "memory_relations", "relations", "memory_entities", "entities",
                              "memory_vectors", "memory_sources", "memory_versions", "write_decisions",
                              "memories", "profiles"):
                    self.db.execute(f"DELETE FROM {table}")
                self.db.execute(
                    "UPDATE outbox SET status='PENDING', attempts=0, last_error=NULL, claimed_at=NULL, processed_at=NULL "
                    "WHERE event_id NOT IN (SELECT object_id FROM tombstones WHERE object_type='event')"
                )
                return

            memory_ids = [row[0] for row in self.db.execute(
                "SELECT id FROM memories WHERE namespace=?", (namespace,)).fetchall()]
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                for table, column in (("memory_fts", "memory_id"), ("memory_access", "memory_id"),
                                      ("memory_relations", "memory_id"), ("memory_entities", "memory_id"),
                                      ("memory_vectors", "memory_id"), ("memory_sources", "memory_id"),
                                      ("memory_versions", "memory_id"), ("memories", "id")):
                    self.db.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", memory_ids)
            self.db.execute("DELETE FROM relations WHERE namespace=?", (namespace,))
            self.db.execute("DELETE FROM entities WHERE namespace=?", (namespace,))
            self.db.execute("DELETE FROM profiles WHERE namespace=?", (namespace,))
            self.db.execute(
                "DELETE FROM write_decisions WHERE event_id IN (SELECT id FROM events WHERE namespace=?)",
                (namespace,),
            )
            self.db.execute(
                "UPDATE outbox SET status='PENDING', attempts=0, last_error=NULL, claimed_at=NULL, processed_at=NULL "
                "WHERE event_id IN (SELECT id FROM events WHERE namespace=?) "
                "AND event_id NOT IN (SELECT object_id FROM tombstones WHERE object_type='event')",
                (namespace,),
            )

    def reindex_lexical(self, namespace: str | None = None) -> int:
        """Rebuild the FTS projection from ``memories``.

        The FTS table is a derived projection (see docs/05), so it must be
        rebuildable — this is also how a database written before CJK bigram
        indexing becomes searchable again.
        """
        clause = "WHERE namespace=? AND status != 'DELETED'" if namespace else "WHERE status != 'DELETED'"
        params = (namespace,) if namespace else ()
        rows = self.db.execute("SELECT id, content, structured_content FROM memories " + clause, params).fetchall()
        documents = [(row["id"], lexical.index_document(row["content"]),
                      lexical.structured_document(json.loads(row["structured_content"]))) for row in rows]
        with self.transaction():
            # memory_fts has no namespace column, so a scoped rebuild must
            # delete only the rows owned by that namespace.
            if namespace:
                self.db.execute("DELETE FROM memory_fts WHERE memory_id IN "
                                "(SELECT id FROM memories WHERE namespace=?)", (namespace,))
            else:
                self.db.execute("DELETE FROM memory_fts")
            for document in documents:
                self.db.execute("INSERT INTO memory_fts VALUES (?,?,?)", document)
        return len(rows)

    def reindex_vectors(self, namespace: str | None = None) -> int:
        if namespace is not None and not self._vector_index_compatible:
            raise RuntimeError("model changes require a full reindex across all namespaces")
        clause = "WHERE namespace=? AND status != 'DELETED'" if namespace else "WHERE status != 'DELETED'"
        params = (namespace,) if namespace else ()
        rows = self.db.execute("SELECT id, content FROM memories " + clause, params).fetchall()
        vectors: list[tuple[str, str]] = []
        for row in rows:
            vector = self.embedder.embed(row["content"])
            if len(vector) != self.embedder.dimensions:
                raise ValueError(
                    f"embedding dimensions do not match provider: expected {self.embedder.dimensions}, got {len(vector)}"
                )
            vectors.append((row["id"], json.dumps(vector)))
        with self.transaction():
            if namespace:
                self.db.execute("DELETE FROM memory_vectors WHERE memory_id IN "
                                "(SELECT id FROM memories WHERE namespace=? AND status='DELETED')", (namespace,))
            else:
                self.db.execute("DELETE FROM memory_vectors WHERE memory_id IN "
                                "(SELECT id FROM memories WHERE status='DELETED')")
            for memory_id, vector_json in vectors:
                self.db.execute("INSERT OR REPLACE INTO memory_vectors(memory_id, vector) VALUES (?, ?)",
                                (memory_id, vector_json))
            if namespace is None:
                self.db.execute("INSERT OR REPLACE INTO vector_metadata(id, model, dimensions) VALUES (1, ?, ?)",
                                (self.embedder.model, self.embedder.dimensions))
        if namespace is None:
            self._vector_index_compatible = True
        return len(rows)

    def attach_entity(self, namespace: str, memory_id: str, canonical_name: str, entity_type: str,
                      role: str = "mentioned") -> int:
        self.db.execute("INSERT OR IGNORE INTO entities(namespace, canonical_name, entity_type) VALUES (?,?,?)",
                        (namespace, canonical_name, entity_type))
        entity_id = self.db.execute("SELECT id FROM entities WHERE namespace=? AND canonical_name=?",
                                    (namespace, canonical_name)).fetchone()[0]
        self.db.execute("INSERT OR IGNORE INTO memory_entities VALUES (?,?,?)", (memory_id, entity_id, role))
        self._commit()
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
        self._commit()
        return int(relation_id)

    def update_status(self, memory_id: str, status: MemoryStatus) -> None:
        self.db.execute("UPDATE memories SET status=? WHERE id=?", (status.value, memory_id))
        self._commit()

    def merge_memory(self, survivor_id: str, duplicate_id: str) -> None:
        """Merge provenance into the survivor and retire duplicate indexes."""
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO memory_sources(memory_id,event_id) "
                "SELECT ?, event_id FROM memory_sources WHERE memory_id=?",
                (survivor_id, duplicate_id),
            )
            self.db.execute(
                "UPDATE memories SET status='MERGED', updated_at=? WHERE id=?", (utc_now(), duplicate_id),
            )
            self._delete_memory_projections(duplicate_id)

    def update_importance(self, memory_id: str, importance: float) -> None:
        self.db.execute("UPDATE memories SET importance=?, updated_at=? WHERE id=?", (importance, utc_now(), memory_id))
        self._commit()

    def reinforce_memory(self, memory_id: str, *, importance_delta: float = 0.05,
                         confidence_delta: float = 0.02) -> None:
        self.db.execute(
            "UPDATE memories SET status='REINFORCED', importance=MIN(1.0,importance+?), "
            "confidence=MIN(1.0,confidence+?), updated_at=? "
            "WHERE id=? AND status IN ('ACTIVE','ARCHIVED','REINFORCED')",
            (importance_delta, confidence_delta, utc_now(), memory_id),
        )
        self._commit()

    def upsert_profile(self, namespace: str, current: dict, generated_from_version: int, updated_at: str) -> None:
        self.db.execute(
            "INSERT INTO profiles(namespace,schema_version,current_json,generated_from_version,updated_at) VALUES (?,?,?, ?,?) "
            "ON CONFLICT(namespace) DO UPDATE SET current_json=excluded.current_json, generated_from_version=excluded.generated_from_version, updated_at=excluded.updated_at",
            (namespace, 1, json.dumps(current, ensure_ascii=False), generated_from_version, updated_at),
        )
        self._commit()

    def get_profile(self, namespace: str) -> dict | None:
        row = self.db.execute("SELECT * FROM profiles WHERE namespace=?", (namespace,)).fetchone()
        if not row:
            return None
        value = json.loads(row["current_json"])
        value["_meta"] = {"schema_version": row["schema_version"], "generated_from_version": row["generated_from_version"], "updated_at": row["updated_at"]}
        return value

    def close_validity(self, memory_id: str, valid_to: str) -> None:
        self.db.execute("UPDATE memories SET valid_to=?, updated_at=? WHERE id=?", (valid_to, valid_to, memory_id))
        self.db.execute(
            "UPDATE relations SET valid_to=? WHERE id IN "
            "(SELECT relation_id FROM memory_relations WHERE memory_id=?)",
            (valid_to, memory_id),
        )
        self._commit()

    def active_memories(self, namespace: str) -> list[Memory]:
        # Ordered so that conflict detection and consolidation pick a stable
        # survivor instead of depending on SQLite's unspecified row order.
        rows = self.db.execute(
            "SELECT * FROM memories WHERE namespace=? AND status IN ('ACTIVE','REINFORCED') "
            "ORDER BY observed_at, created_at, id",
            (namespace,)).fetchall()
        return [self._memory(row) for row in rows]

    def search(self, namespace: str, query: str, limit: int = 20, as_of: str | None = None,
               include_history: bool = False, predicates: Iterable[str] | None = None,
               temporal_mode: str = "any", observed_as_of: str | None = None,
               multi_hop: bool = False
               ) -> list[tuple[Memory, float, list[str]]]:
        query_vec = self.embedder.embed(query)
        predicate_filter = set(predicates or ())
        status_clause = "m.status IN ('ACTIVE','REINFORCED')" if not include_history else "m.status NOT IN ('DELETED','MERGED')"
        lexical_scores: dict[str, float] = {}
        match_query = lexical.match_expression(query)
        if match_query:
            try:
                hits = self.db.execute(
                    "SELECT f.memory_id FROM memory_fts f JOIN memories m ON m.id=f.memory_id "
                    "WHERE memory_fts MATCH ? AND m.namespace=? AND " + status_clause +
                    " ORDER BY bm25(memory_fts), f.memory_id",
                    (match_query, namespace),
                ).fetchall()
                for position, hit in enumerate(hits):
                    lexical_scores[hit["memory_id"]] = 1.0 / (1.0 + position)
            except sqlite3.OperationalError:
                pass
        rows = self.db.execute(
            "SELECT m.*, v.vector FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
            "WHERE m.namespace=? AND " + status_clause + " ORDER BY m.observed_at, m.id",
            (namespace,),
        ).fetchall()
        entity_matches = {row["memory_id"] for row in self.db.execute(
            "SELECT me.memory_id FROM memory_entities me JOIN entities e ON e.id=me.entity_id "
            "WHERE e.namespace=? AND instr(?, e.canonical_name) > 0", (namespace, query.lower()))}
        relation_matches = {row["memory_id"] for row in self.db.execute(
            "SELECT mr.memory_id FROM memory_relations mr JOIN relations r ON r.id=mr.relation_id "
            "JOIN entities s ON s.id=r.subject_entity_id JOIN entities o ON o.id=r.object_entity_id "
            "WHERE r.namespace=? AND (instr(?, s.canonical_name)>0 OR instr(?, o.canonical_name)>0 OR instr(?, r.predicate)>0)",
            (namespace, query.lower(), query.lower(), query.lower()))}
        multi_hop_matches: set[str] = set()
        if multi_hop:
            frontier = {row["id"] for row in self.db.execute(
                "SELECT id FROM entities WHERE namespace=? AND canonical_name NOT LIKE 'user:%' "
                "AND instr(?, canonical_name)>0 LIMIT 20", (namespace, query.lower()))}
            visited = set(frontier)
            relation_ids: set[int] = set()
            for _ in range(2):
                if not frontier:
                    break
                placeholders = ",".join("?" for _ in frontier)
                edges = self.db.execute(
                    f"SELECT id,subject_entity_id,object_entity_id FROM relations WHERE namespace=? AND "
                    f"(subject_entity_id IN ({placeholders}) OR object_entity_id IN ({placeholders})) LIMIT 200",
                    (namespace, *frontier, *frontier),
                ).fetchall()
                next_frontier: set[int] = set()
                for edge in edges:
                    relation_ids.add(int(edge["id"]))
                    next_frontier.update((int(edge["subject_entity_id"]), int(edge["object_entity_id"])))
                frontier = next_frontier - visited
                visited.update(next_frontier)
            if relation_ids:
                placeholders = ",".join("?" for _ in relation_ids)
                multi_hop_matches = {row["memory_id"] for row in self.db.execute(
                    f"SELECT memory_id FROM memory_relations WHERE relation_id IN ({placeholders})",
                    tuple(relation_ids),
                )}
        candidates: list[dict] = []
        for row in rows:
            memory = self._memory(row)
            if not temporal.visible_at(memory.valid_from, memory.valid_to, as_of):
                continue
            observed_cutoff = temporal.parse_instant(observed_as_of)
            observed_at = temporal.parse_instant(memory.observed_at)
            if observed_cutoff is not None and observed_at is not None and observed_at > observed_cutoff:
                continue
            stored_vector = json.loads(row["vector"])
            dense = cosine(query_vec, stored_vector) if self._vector_index_compatible and len(stored_vector) == len(query_vec) else 0.0
            sparse = lexical_scores.get(memory.id, 0.0)
            candidates.append({"memory": memory, "dense": dense, "bm25": sparse,
                               "entity": memory.id in entity_matches,
                               "relation": memory.id in relation_matches,
                               "multi_hop": memory.id in multi_hop_matches,
                               "predicate": memory.structured_content.get("predicate") in predicate_filter})

        # Reciprocal-rank fusion keeps incompatible channel scales separate.
        # Each channel ranks the candidates it actually supports; a single weak
        # deterministic-vector collision is filtered before fusion.
        ranks: dict[str, dict[str, int]] = {}
        for channel in ("dense", "bm25"):
            ordered = sorted(
                (candidate for candidate in candidates if candidate[channel] > 0),
                key=lambda candidate: (-candidate[channel], candidate["memory"].id),
            )
            ranks[channel] = {candidate["memory"].id: rank for rank, candidate in enumerate(ordered, start=1)}
        for channel in ("entity", "relation", "multi_hop", "predicate"):
            ordered = sorted(
                (candidate for candidate in candidates if candidate[channel]),
                key=lambda candidate: candidate["memory"].id,
            )
            ranks[channel] = {candidate["memory"].id: rank for rank, candidate in enumerate(ordered, start=1)}

        scored: list[tuple[Memory, float, list[str]]] = []
        for candidate in candidates:
            memory = candidate["memory"]
            channels = [channel for channel in ("dense", "bm25", "entity", "relation", "multi_hop", "predicate")
                        if memory.id in ranks[channel]]
            if not channels:
                continue
            if channels == ["dense"]:
                if not getattr(self.embedder, "semantic_similarity", True):
                    continue
                if candidate["dense"] < (0.15 / 0.55):
                    continue
            rrf = sum(1.0 / (60 + ranks[channel][memory.id]) for channel in channels)
            # Lightweight deterministic rerank. RRF remains dominant while
            # evidence quality breaks close ties without mixing raw channel
            # scales back into the fusion score.
            quality = 0.8 + 0.1 * memory.importance + 0.1 * memory.confidence
            score = rrf * quality
            scored.append((memory, score, channels))
        if temporal_mode in {"earliest", "latest"}:
            def time_key(item: tuple[Memory, float, list[str]]) -> datetime:
                memory = item[0]
                return temporal.parse_instant(memory.valid_from or memory.observed_at) or datetime.min.replace(
                    tzinfo=timezone.utc)

            scored.sort(key=lambda item: (time_key(item), -item[1], item[0].id),
                        reverse=temporal_mode == "latest")
        else:
            scored.sort(key=lambda item: (-item[1], item[0].id))
        return scored[:limit]

    def tombstone_memory(self, memory_id: str, reason: str, deleted_at: str) -> None:
        with self.transaction():
            row = self.db.execute("SELECT namespace FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            self.db.execute(
                "INSERT OR REPLACE INTO tombstones(object_type,object_id,reason,deleted_at,namespace) "
                "VALUES ('memory',?,?,?,?)", (memory_id, reason, deleted_at, row["namespace"]),
            )
            self.db.execute("UPDATE memories SET status='DELETED', updated_at=? WHERE id=?", (deleted_at, memory_id))
            self._delete_memory_projections(memory_id)

    def _delete_memory_projections(self, memory_id: str) -> None:
        self.db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        self.db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (memory_id,))
        self.db.execute("DELETE FROM memory_relations WHERE memory_id=?", (memory_id,))
        self.db.execute("DELETE FROM memory_entities WHERE memory_id=?", (memory_id,))
        self.db.execute("DELETE FROM relations WHERE id NOT IN (SELECT relation_id FROM memory_relations)")
        self.db.execute("DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM memory_entities)")

    def purge_memory(self, memory_id: str, reason: str, deleted_at: str) -> None:
        """Irreversibly remove content while retaining a content-free audit tombstone."""
        with self.transaction():
            row = self.db.execute("SELECT namespace FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            self.db.execute(
                "INSERT OR REPLACE INTO tombstones(object_type,object_id,reason,deleted_at,namespace) "
                "VALUES ('memory',?,?,?,?)", (memory_id, reason, deleted_at, row["namespace"]),
            )
            self._delete_memory_projections(memory_id)
            self.db.execute("DELETE FROM memory_access WHERE memory_id=?", (memory_id,))
            self.db.execute("DELETE FROM memory_sources WHERE memory_id=?", (memory_id,))
            self.db.execute("DELETE FROM memory_versions WHERE memory_id=?", (memory_id,))
            self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def record_access(self, memory_id: str, query: str, rank: int, score: float, accessed_at: str, used: bool = False) -> None:
        self.db.execute("INSERT INTO memory_access(memory_id,query,rank,score,used,accessed_at) VALUES (?,?,?,?,?,?)",
                        (memory_id, query, rank, score, int(used), accessed_at))
        self._commit()

    def memories_for_event(self, event_id: str) -> list[Memory]:
        rows = self.db.execute("SELECT m.* FROM memories m JOIN memory_sources s ON s.memory_id=m.id WHERE s.event_id=?", (event_id,)).fetchall()
        return [self._memory(row) for row in rows]

    def _reassign_relation_source(self, memory_id: str, old_event_id: str, new_event_id: str) -> None:
        relations = self.db.execute(
            "SELECT r.* FROM relations r JOIN memory_relations mr ON mr.relation_id=r.id "
            "WHERE mr.memory_id=? AND r.source_event_id=?", (memory_id, old_event_id),
        ).fetchall()
        for relation in relations:
            existing = self.db.execute(
                "SELECT id FROM relations WHERE namespace=? AND subject_entity_id=? AND predicate=? "
                "AND object_entity_id=? AND source_event_id=?",
                (relation["namespace"], relation["subject_entity_id"], relation["predicate"],
                 relation["object_entity_id"], new_event_id),
            ).fetchone()
            if existing:
                self.db.execute("INSERT OR IGNORE INTO memory_relations VALUES (?,?)", (memory_id, existing["id"]))
                self.db.execute("DELETE FROM memory_relations WHERE memory_id=? AND relation_id=?",
                                (memory_id, relation["id"]))
                self.db.execute("DELETE FROM relations WHERE id=? AND id NOT IN "
                                "(SELECT relation_id FROM memory_relations)", (relation["id"],))
            else:
                self.db.execute("UPDATE relations SET source_event_id=? WHERE id=?",
                                (new_event_id, relation["id"]))

    def tombstone_event(self, event_id: str, reason: str, deleted_at: str, *, hard: bool = False) -> list[str]:
        with self.transaction():
            event = self.get_event(event_id)
            if event is None:
                raise KeyError(event_id)
            affected = self.memories_for_event(event_id)
            deleted_ids: list[str] = []
            for memory in affected:
                source_count = self.db.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE memory_id=?", (memory.id,)).fetchone()[0]
                if source_count <= 1:
                    if hard:
                        self.purge_memory(memory.id, reason, deleted_at)
                    else:
                        self.tombstone_memory(memory.id, reason, deleted_at)
                    deleted_ids.append(memory.id)
                else:
                    if hard:
                        replacement = self.db.execute(
                            "SELECT event_id FROM memory_sources WHERE memory_id=? AND event_id<>? "
                            "ORDER BY event_id LIMIT 1", (memory.id, event_id),
                        ).fetchone()
                        if replacement:
                            self._reassign_relation_source(memory.id, event_id, replacement["event_id"])
                    self.db.execute("DELETE FROM memory_sources WHERE memory_id=? AND event_id=?", (memory.id, event_id))
            self.db.execute(
                "INSERT OR REPLACE INTO tombstones(object_type,object_id,reason,deleted_at,namespace) "
                "VALUES ('event',?,?,?,?)", (event_id, reason, deleted_at, event.namespace),
            )
            # A deleted deferred event must never be replayed by the outbox worker.
            self.db.execute(
                "UPDATE outbox SET status='DONE', processed_at=?, claimed_at=NULL "
                "WHERE event_id=? AND status IN ('PENDING','FAILED','PROCESSING')",
                (deleted_at, event_id),
            )
            # Preserve source events by default so provenance and audit remain
            # traversable. Hard purge is a separate explicit retention workflow.
            if hard:
                self.db.execute(
                    "DELETE FROM memory_relations WHERE relation_id IN "
                    "(SELECT id FROM relations WHERE source_event_id=?)", (event_id,),
                )
                self.db.execute("DELETE FROM relations WHERE source_event_id=?", (event_id,))
                self.db.execute("DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM memory_entities)")
                self.db.execute("UPDATE memory_versions SET source_event_id=NULL WHERE source_event_id=?", (event_id,))
                self.db.execute("DELETE FROM memory_sources WHERE event_id=?", (event_id,))
                self.db.execute("DELETE FROM write_decisions WHERE event_id=?", (event_id,))
                self.db.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
                self.db.execute("DELETE FROM events WHERE id=?", (event_id,))
        return deleted_ids

    def events_for_namespace(self, namespace: str) -> Iterable[Event]:
        yield from (event for event in self.all_events() if event.namespace == namespace)

    def all_events(self) -> list[Event]:
        events = [event for row in self.db.execute("SELECT id FROM events")
                  if (event := self.get_event(row["id"])) is not None]
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(events, key=lambda event: (
            temporal.parse_instant(event.observed_at) or minimum,
            temporal.parse_instant(event.created_at) or minimum,
            event.id,
        ))

    def memories_for_namespace(self, namespace: str, *, include_deleted: bool = False) -> list[Memory]:
        clause = "" if include_deleted else " AND status != 'DELETED'"
        rows = self.db.execute(
            "SELECT * FROM memories WHERE namespace=?" + clause + " ORDER BY observed_at, created_at, id",
            (namespace,)).fetchall()
        return [self._memory(row) for row in rows]
