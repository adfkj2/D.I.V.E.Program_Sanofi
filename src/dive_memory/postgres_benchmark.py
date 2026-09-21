from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from statistics import median
from time import perf_counter
import uuid

from .benchmark import _percentile
from .ids import stable_vector
from .postgres_migrations import apply_migrations
from .retrieval import RetrievalConfig
from .service import MemoryService


class BenchmarkEmbeddingProvider:
    """Synthetic semantic envelope used to exercise pgvector exact search.

    The vectors are deterministic hash fixtures, not a quality model. Results
    from this benchmark describe storage and query plumbing only.
    """

    dimensions = 96
    model = "benchmark-hash-v1"
    semantic_similarity = True
    revision = "v1"
    provider_name = "postgres-benchmark-fixture"

    def embed(self, text: str) -> list[float]:
        return stable_vector(text, dimensions=self.dimensions)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50": median(values),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def run_postgres_benchmark(dsn: str, memory_count: int = 10_000,
                           query_iterations: int = 20) -> dict:
    """Measure the opt-in PostgreSQL exact-vector reference path.

    A unique schema is migrated for each run and dropped afterward. The DSN is
    never included in the result.
    """

    try:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
    except ImportError as exc:  # pragma: no cover - optional environment
        raise RuntimeError("install dive-memory[postgres] to run this benchmark") from exc

    from .postgres_store import PostgresStore

    memory_count = max(1, int(memory_count))
    query_iterations = max(1, int(query_iterations))
    schema = f"dive_benchmark_{uuid.uuid4().hex}"
    migration_root = Path(__file__).parents[2] / "migrations"
    run_started = datetime.now(timezone.utc).isoformat()

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        store = None
        try:
            benchmark_dsn = make_conninfo(dsn, options=f"-c search_path={schema},public")
            with psycopg.connect(benchmark_dsn, autocommit=False) as migration_connection:
                applied_migrations = apply_migrations(migration_connection, migration_root)

            store = PostgresStore(benchmark_dsn, embedder=BenchmarkEmbeddingProvider())
            service = MemoryService(store=store)
            namespace = "pg-benchmark"
            ingest_ms: list[float] = []
            memory_ids: list[str] = []

            total_started = perf_counter()
            for index in range(memory_count):
                text = f"请记住项目事实 pg-project-{index}"
                started = perf_counter()
                result = service.ingest(
                    namespace,
                    text,
                    explicit=True,
                    observed_at="2026-01-01T00:00:00+00:00",
                    idempotency_key=f"pg-benchmark-{index}",
                )
                ingest_ms.append((perf_counter() - started) * 1000)
                memory_ids.extend(result["memory_ids"])
            ingest_total_seconds = perf_counter() - total_started

            dense_only = RetrievalConfig(frozenset({"dense"}), name="postgres-exact-dense")
            service.retrieve(
                namespace,
                "请记住项目事实 pg-project-0",
                limit=5,
                config=dense_only,
            )
            query_ms: list[float] = []
            exact_hits = 0
            last_result = None
            for index in range(query_iterations):
                target = (index * max(1, memory_count // query_iterations)) % memory_count
                started = perf_counter()
                last_result = service.retrieve(
                    namespace,
                    f"请记住项目事实 pg-project-{target}",
                    limit=5,
                    config=dense_only,
                )
                query_ms.append((perf_counter() - started) * 1000)
                if last_result.items and last_result.items[0].memory.id == memory_ids[target]:
                    exact_hits += 1

            sizes = store.connection.execute(
                "SELECT COALESCE(sum(pg_total_relation_size((quote_ident(schemaname)||'.'||"
                "quote_ident(tablename))::regclass)),0)::bigint AS total_bytes,"
                "COALESCE(sum(pg_indexes_size((quote_ident(schemaname)||'.'||"
                "quote_ident(tablename))::regclass)),0)::bigint AS index_bytes "
                "FROM pg_tables WHERE schemaname=current_schema()"
            ).fetchone()
            server_version = store.connection.execute("SHOW server_version").fetchone()["server_version"]
            pgvector_version = store.connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()["extversion"]

            return {
                "schema_version": "dive-postgres-exact-benchmark-v1",
                "started_at": run_started,
                "backend": "postgresql-pgvector",
                "server_version": server_version,
                "pgvector_version": pgvector_version,
                "search_mode": "exact-cosine-dense-only",
                "embedding": {
                    "provider": BenchmarkEmbeddingProvider.provider_name,
                    "model": BenchmarkEmbeddingProvider.model,
                    "dimensions": BenchmarkEmbeddingProvider.dimensions,
                    "quality_claim": False,
                },
                "memory_count": memory_count,
                "query_iterations": query_iterations,
                "applied_migrations": applied_migrations,
                "ingest_ms": _summary(ingest_ms),
                "ingest_total_seconds": ingest_total_seconds,
                "ingest_per_second": memory_count / ingest_total_seconds,
                "retrieve_ms": _summary(query_ms),
                "exact_query_top1_rate": exact_hits / query_iterations,
                "storage_bytes": int(sizes["total_bytes"]),
                "index_bytes": int(sizes["index_bytes"]),
                "trace_backend": last_result.trace["channels"]["dense"]["backend"],
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                },
                "limitations": [
                    "synthetic hash vectors; no semantic quality claim",
                    "dense-only exact search; no ANN index",
                    "single client and single namespace",
                    "container RSS not captured",
                ],
            }
        finally:
            if store is not None:
                store.close()
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the opt-in PostgreSQL exact-search benchmark")
    parser.add_argument("--memories", type=int, default=10_000)
    parser.add_argument("--queries", type=int, default=20)
    args = parser.parse_args()
    dsn = os.environ.get("DIVE_TEST_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("set DIVE_TEST_POSTGRES_DSN to an isolated PostgreSQL test database")
    print(json.dumps(
        run_postgres_benchmark(dsn, args.memories, args.queries),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
