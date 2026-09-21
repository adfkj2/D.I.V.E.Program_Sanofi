import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("DIVE_TEST_POSTGRES_DSN"),
    reason="set DIVE_TEST_POSTGRES_DSN for the opt-in PostgreSQL suite",
)


def _service():
    pytest.importorskip("psycopg")
    from dive_memory.postgres_store import PostgresStore
    from dive_memory.service import MemoryService

    store = PostgresStore(os.environ["DIVE_TEST_POSTGRES_DSN"])
    return MemoryService(store=store)


@pytest.fixture
def isolated_postgres_dsn():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from dive_memory.postgres_migrations import apply_migrations

    schema = f"dive_store_test_{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["DIVE_TEST_POSTGRES_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(
                os.environ["DIVE_TEST_POSTGRES_DSN"],
                options=f"-c search_path={schema},public",
            )
            with psycopg.connect(dsn, autocommit=False) as connection:
                apply_migrations(connection, Path(__file__).parents[3] / "migrations")
            yield dsn
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


class _SemanticFixtureEmbedding:
    dimensions = 3
    model = "semantic-fixture-v1"
    semantic_similarity = True
    revision = "v1"
    provider_name = "integration-fixture"

    def embed(self, text: str) -> list[float]:
        lowered = text.casefold()
        if "needle" in lowered:
            return [1.0, 0.0, 0.0]
        if "hay" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def test_postgres_service_ingest_retrieve_temporal_transition_and_hard_purge():
    service = _service()
    namespace = f"pg-u-{uuid.uuid4()}"
    try:
        old = service.ingest(namespace, "我住在成都", explicit=True,
                             observed_at="2025-01-01T00:00:00+00:00")
        new = service.ingest(namespace, "我住在上海", explicit=True,
                             observed_at="2026-01-01T00:00:00+00:00")

        assert service.retrieve(namespace, "我现在住在哪里").items[0].memory.id == new["memory_ids"][0]
        assert service.retrieve(namespace, "2025年我住在哪里").items[0].memory.id == old["memory_ids"][0]
        transition = service.store.transitions_for_memory(new["memory_ids"][0])[-1]
        assert transition["relationship"] == "temporal_update"

        service.forget(old["memory_ids"][0], hard=True)
        with service.store.connection.cursor() as cursor:
            for table, column in (
                ("memories", "id"), ("memory_vectors", "memory_id"),
                ("memory_sources", "memory_id"), ("memory_versions", "memory_id"),
                ("memory_keys", "memory_id"), ("memory_access", "memory_id"),
                ("memory_entities", "memory_id"), ("memory_relations", "memory_id"),
            ):
                assert cursor.execute(
                    f"SELECT 1 FROM {table} WHERE {column}=%s", (old["memory_ids"][0],)
                ).fetchone() is None
            assert cursor.execute(
                "SELECT 1 FROM memory_transitions WHERE from_memory_id=%s OR to_memory_id=%s",
                (old["memory_ids"][0], old["memory_ids"][0]),
            ).fetchone() is None
            assert cursor.execute(
                "SELECT 1 FROM relations WHERE source_event_id=%s", (old["event_id"],)
            ).fetchone() is None
            assert cursor.execute(
                "SELECT 1 FROM tombstones WHERE object_type='memory' AND object_id=%s",
                (old["memory_ids"][0],),
            ).fetchone() is not None
    finally:
        service.store.purge_namespace(namespace)
        service.store.close()


def test_postgres_store_namespace_isolation_and_duplicate_provenance():
    service = _service()
    first_namespace = f"pg-a-{uuid.uuid4()}"
    second_namespace = f"pg-b-{uuid.uuid4()}"
    try:
        first = service.ingest(first_namespace, "请记住我喜欢绿茶", explicit=True)
        duplicate = service.ingest(first_namespace, "请记住我喜欢绿茶", explicit=True)
        other = service.ingest(second_namespace, "请记住我喜欢咖啡", explicit=True)

        assert first["memory_ids"] == duplicate["memory_ids"]
        assert len(service.get_memory(first["memory_ids"][0]).source_event_ids) == 2
        assert other["memory_ids"][0] not in {
            item.memory.id for item in service.retrieve(first_namespace, "咖啡").items
        }
    finally:
        service.store.purge_namespace(first_namespace)
        service.store.purge_namespace(second_namespace)
        service.store.close()


def test_postgres_store_uses_pgvector_exact_cosine_search(isolated_postgres_dsn):
    from dive_memory.postgres_store import PostgresStore
    from dive_memory.retrieval import RetrievalConfig
    from dive_memory.service import MemoryService

    store = PostgresStore(isolated_postgres_dsn, embedder=_SemanticFixtureEmbedding())
    service = MemoryService(store=store)
    namespace = f"pg-vector-{uuid.uuid4()}"
    try:
        needle = service.ingest(namespace, "请记住项目事实 needle", explicit=True)
        service.ingest(namespace, "请记住项目事实 hay", explicit=True)
        result = service.retrieve(
            namespace,
            "needle",
            config=RetrievalConfig(frozenset({"dense"}), name="dense-exact-test"),
        )

        assert result.items[0].memory.id == needle["memory_ids"][0]
        assert result.items[0].channels == ["dense"]
        assert result.trace["channels"]["dense"]["backend"] == "postgres-pgvector-exact-cosine"
    finally:
        store.purge_namespace(namespace)
        store.close()


def test_postgres_projection_transaction_rolls_back_and_marks_event_failed():
    service = _service()
    namespace = f"pg-rollback-{uuid.uuid4()}"
    original = service.store.record_transition

    def fail_transition(**_kwargs):
        raise RuntimeError("injected transition failure")

    service.store.record_transition = fail_transition
    try:
        with pytest.raises(RuntimeError, match="injected transition failure"):
            service.ingest(namespace, "请记住项目事实 rollback", explicit=True)

        event = service.store.connection.execute(
            "SELECT id FROM events WHERE namespace=%s", (namespace,)
        ).fetchone()
        outbox = service.store.connection.execute(
            "SELECT status,attempts FROM outbox WHERE event_id=%s", (event["id"],)
        ).fetchone()
        assert service.store.connection.execute(
            "SELECT 1 FROM memories WHERE namespace=%s", (namespace,)
        ).fetchone() is None
        assert outbox == {"status": "FAILED", "attempts": 1}
    finally:
        service.store.record_transition = original
        service.store.purge_namespace(namespace)
        service.store.close()


def test_postgres_concurrent_idempotency_creates_one_event_and_projection():
    namespace = f"pg-idempotent-{uuid.uuid4()}"
    key = f"key-{uuid.uuid4()}"
    barrier = threading.Barrier(2)

    def ingest_once():
        service = _service()
        try:
            barrier.wait(timeout=5)
            return service.ingest(
                namespace,
                "请记住项目事实 concurrent-idempotency",
                explicit=True,
                idempotency_key=key,
            )
        finally:
            service.store.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: ingest_once(), range(2)))

        assert sorted(result["accepted"] for result in results) == [False, True]
        assert len({result["event_id"] for result in results}) == 1

        verifier = _service()
        try:
            assert verifier.store.connection.execute(
                "SELECT count(*) AS count FROM events WHERE namespace=%s", (namespace,)
            ).fetchone()["count"] == 1
            assert verifier.store.connection.execute(
                "SELECT count(*) AS count FROM memories WHERE namespace=%s", (namespace,)
            ).fetchone()["count"] == 1
        finally:
            verifier.store.close()
    finally:
        cleanup = _service()
        cleanup.store.purge_namespace(namespace)
        cleanup.store.close()
