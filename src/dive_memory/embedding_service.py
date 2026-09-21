from __future__ import annotations

import json

from .embeddings import EmbeddingProvider, embed_with_metadata
from .models import utc_now
from .store import SQLiteStore


class EmbeddingLifecycleError(RuntimeError):
    pass


class EmbeddingLifecycle:
    """Resumable embedding backfill and atomic active-generation cutover."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        active = store.db.execute("SELECT active_generation FROM vector_metadata WHERE id=1").fetchone()[0]
        self._providers: dict[str, EmbeddingProvider] = {active: store.embedder}
        # Preserve the current generation as a rollback source before another
        # generation replaces the compatibility projection.
        store.db.execute(
            "INSERT OR IGNORE INTO embedding_vector_staging(memory_id,generation_id,vector,degraded,provider,model,"
            "revision,dimensions,updated_at) "
            "SELECT v.memory_id,?,v.vector,COALESCE(s.degraded,0),COALESCE(s.provider,?),COALESCE(s.model,?),"
            "COALESCE(s.revision,?),COALESCE(s.dimensions,?),? FROM memory_vectors v "
            "LEFT JOIN memory_vector_state s ON s.memory_id=v.memory_id",
            (active, str(getattr(store.embedder, "provider_name", type(store.embedder).__name__)),
             store.embedder.model, str(getattr(store.embedder, "revision", "unversioned")),
             store.embedder.dimensions, utc_now()),
        )
        store.db.commit()

    def prepare(self, provider: EmbeddingProvider) -> str:
        generation = self.store.embedding_generation_id(provider)
        self._providers[generation] = provider
        self.store.db.execute(
            "INSERT OR IGNORE INTO embedding_generations(id,provider,model,revision,dimensions,status,created_at) "
            "VALUES (?,?,?,?,?,'PENDING',?)",
            (generation, str(getattr(provider, "provider_name", type(provider).__name__)), provider.model,
             str(getattr(provider, "revision", "unversioned")), provider.dimensions, utc_now()),
        )
        self.store.db.commit()
        return generation

    def backfill(self, generation: str, *, namespace: str | None = None) -> int:
        provider = self._providers.get(generation)
        if provider is None:
            raise EmbeddingLifecycleError("generation provider is not registered in this process")
        clause = "AND namespace=?" if namespace else ""
        params = (namespace,) if namespace else ()
        rows = self.store.db.execute(
            "SELECT id,content FROM memories WHERE status!='DELETED' " + clause + " ORDER BY id", params,
        ).fetchall()
        completed = 0
        for row in rows:
            result = embed_with_metadata(provider, row["content"])
            self.store.db.execute(
                "INSERT OR REPLACE INTO embedding_vector_staging(memory_id,generation_id,vector,degraded,provider,"
                "model,revision,dimensions,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], generation, json.dumps(result.vector), int(result.degraded), result.provider,
                 result.model, result.revision, result.dimensions, utc_now()),
            )
            self.store.db.commit()
            completed += 1
        self.store.db.execute("UPDATE embedding_generations SET status='READY' WHERE id=?", (generation,))
        self.store.db.commit()
        return completed

    def activate(self, generation: str) -> None:
        provider = self._providers.get(generation)
        if provider is None:
            raise EmbeddingLifecycleError("generation provider is not registered in this process")
        if not bool(getattr(provider, "semantic_similarity", True)):
            raise EmbeddingLifecycleError("production generation must use a semantic provider")
        expected = {row[0] for row in self.store.db.execute(
            "SELECT id FROM memories WHERE status!='DELETED'"
        )}
        rows = self.store.db.execute(
            "SELECT memory_id,vector,degraded,provider,model,revision,dimensions FROM embedding_vector_staging "
            "WHERE generation_id=?", (generation,),
        ).fetchall()
        available = {row["memory_id"] for row in rows if not row["degraded"]}
        if available != expected:
            missing = len(expected - available)
            extra = len(available - expected)
            raise EmbeddingLifecycleError(f"backfill incomplete: missing={missing}, extra={extra}")
        dimensions = {int(row["dimensions"]) for row in rows}
        if dimensions and dimensions != {provider.dimensions}:
            raise EmbeddingLifecycleError("staged dimensions do not match provider")

        metadata = self.store.db.execute(
            "SELECT active_generation FROM vector_metadata WHERE id=1"
        ).fetchone()
        previous = metadata["active_generation"]
        with self.store.transaction(immediate=True):
            self.store.db.execute("DELETE FROM memory_vectors")
            self.store.db.execute("DELETE FROM memory_vector_state")
            for row in rows:
                self.store.db.execute("INSERT INTO memory_vectors(memory_id,vector) VALUES (?,?)",
                                      (row["memory_id"], row["vector"]))
                self.store.db.execute(
                    "INSERT INTO memory_vector_state(memory_id,generation_id,provider,model,revision,dimensions,"
                    "degraded,updated_at) VALUES (?,?,?,?,?,?,0,?)",
                    (row["memory_id"], generation, row["provider"], row["model"], row["revision"],
                     row["dimensions"], utc_now()),
                )
            self.store.db.execute(
                "UPDATE vector_metadata SET model=?,dimensions=?,provider=?,revision=?,active_generation=?,"
                "previous_generation=? WHERE id=1",
                (provider.model, provider.dimensions,
                 str(getattr(provider, "provider_name", type(provider).__name__)),
                 str(getattr(provider, "revision", "unversioned")), generation, previous),
            )
            self.store.db.execute("UPDATE embedding_generations SET status='RETIRED' WHERE id=?", (previous,))
            self.store.db.execute(
                "UPDATE embedding_generations SET status='ACTIVE',activated_at=? WHERE id=?",
                (utc_now(), generation),
            )
        self.store.embedder = provider
        self.store._vector_index_compatible = True

    def rollback(self) -> None:
        row = self.store.db.execute(
            "SELECT active_generation,previous_generation FROM vector_metadata WHERE id=1"
        ).fetchone()
        previous = row["previous_generation"]
        if not previous:
            raise EmbeddingLifecycleError("no previous generation is available")
        if previous not in self._providers:
            raise EmbeddingLifecycleError("previous generation provider is not registered in this process")
        self.backfill(previous)
        self.activate(previous)

    def assert_production_ready(self) -> None:
        provider = self.store.embedder
        if not bool(getattr(provider, "semantic_similarity", True)):
            raise EmbeddingLifecycleError("production readiness requires a semantic embedding provider")
        row = self.store.db.execute(
            "SELECT COUNT(*) FROM memory_vector_state WHERE degraded=1"
        ).fetchone()
        if row[0]:
            raise EmbeddingLifecycleError("active embedding generation contains degraded vectors")
