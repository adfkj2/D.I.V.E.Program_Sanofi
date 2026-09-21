from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
from statistics import mean, median, pstdev
import subprocess
import sys
from time import perf_counter
import traceback
from typing import Any, Callable

from .benchmark import _percentile
from .ids import stable_vector
from .postgres_benchmark import BenchmarkEmbeddingProvider
from .postgres_migrations import apply_migrations


SCHEMA_VERSION = "dive-postgres-100k-checkpoints-v1"
DATASET_SIZE = 100_000
SEED = 20260920
MAIN_NAMESPACE = "postgres-100k-main"
CHECKPOINTS = {
    "A": "bulk-load.json",
    "B": "retrieval-exact.json",
    "C": "retrieval-filtered.json",
    "D": "retrieval-temporal.json",
    "E": "incremental-ingest.json",
    "F": "delete.json",
    "G": "concurrency.json",
    "H": "mixed-workload.json",
}
PREREQUISITES = {
    "A": (), "B": ("A",), "C": ("A", "B"), "D": ("A", "B", "C"),
    "E": ("A", "B", "C", "D"), "F": ("A", "B", "C", "D", "E"),
    "G": ("A", "B", "C", "D", "E", "F"),
    "H": ("A", "B", "C", "D", "E", "F", "G"),
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return str(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_json_default).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(path)
    json.loads(path.read_text(encoding="utf-8"))
    return _file_sha256(path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0,
                "mean": 0.0, "min": 0.0, "max": 0.0, "stddev": 0.0}
    return {
        "count": len(values), "p50": median(values),
        "p95": _percentile(values, 0.95), "p99": _percentile(values, 0.99),
        "mean": mean(values), "min": min(values), "max": max(values),
        "stddev": pstdev(values) if len(values) > 1 else 0.0,
    }


def _schema_name(run_id: str) -> str:
    suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return f"dive_postgres_100k_{suffix}"


def _expected_config() -> dict[str, Any]:
    return {"memory_count": DATASET_SIZE, "seed": SEED, "namespace": MAIN_NAMESPACE,
            "embedding_dimensions": 96, "synthetic_vectors": True,
            "semantic_quality_claim": False, "ann_indexes": False,
            "exact_queries": 100, "filtered_queries_per_selectivity": 100,
            "temporal_queries_per_treatment": 100}


def _run_command(args: list[str], timeout: int = 15) -> dict[str, Any]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace", check=False)
        return {"available": result.returncode == 0, "returncode": result.returncode,
                "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except Exception as exc:  # pragma: no cover - host dependent
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _host_memory() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "reason": "Windows GlobalMemoryStatusEx only"}

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {"available": False, "reason": "GlobalMemoryStatusEx failed"}
    return {"available": True, "load_percent": int(status.load),
            "total_bytes": int(status.total_phys), "available_bytes": int(status.avail_phys)}


def _client_rss() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False}

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    return ({"available": True, "rss_bytes": int(counters.WorkingSetSize),
             "peak_rss_bytes": int(counters.PeakWorkingSetSize)} if ok else
            {"available": False, "reason": "GetProcessMemoryInfo failed"})


def _resource_snapshot(container: str) -> dict[str, Any]:
    docker = _run_command(["docker", "stats", container, "--no-stream", "--format", "{{json .}}"], 20)
    if docker.get("available") and docker.get("stdout"):
        try:
            docker["parsed"] = json.loads(str(docker["stdout"]).splitlines()[-1])
        except json.JSONDecodeError:
            pass
    return {"timestamp": _utc(), "host_memory": _host_memory(),
            "benchmark_client": _client_rss(), "docker_container": docker}


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    dsn: str
    container: str
    manifest: dict[str, Any]

    @property
    def schema(self) -> str:
        return str(self.manifest["schema"])


class CheckpointFailure(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / "manifest.json"


def _save_manifest(ctx: RunContext) -> None:
    ctx.manifest["updated_at"] = _utc()
    _write_json(_manifest_path(ctx.run_dir), ctx.manifest)


def _new_manifest(run_id: str, schema: str, config: dict[str, Any]) -> dict[str, Any]:
    protocol_hash = _sha256_bytes(_canonical_bytes(config))
    return {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "NOT_COMPLETED",
        "created_at": _utc(), "updated_at": _utc(), "schema": schema,
        "config": config, "config_sha256": protocol_hash, "smoke_test": "NOT_COMPLETED",
        "current_checkpoint": None, "last_successful_checkpoint": None,
        "checkpoints": {key: {"status": "NOT_COMPLETED", "attempts": 0,
                               "artifact": filename}
                        for key, filename in CHECKPOINTS.items()},
        "safety": {"formal_schema_is_persistent": True, "drop_schema_on_failure": False,
                   "ann_indexes_allowed": False, "max_automatic_retries": 0},
    }


def _load_context(run_id: str, reports_root: Path, dsn: str, container: str,
                  *, require_schema: bool = True) -> RunContext:
    run_dir = reports_root / run_id
    manifest = _read_json(_manifest_path(run_dir))
    if manifest.get("run_id") != run_id or manifest.get("schema") != _schema_name(run_id):
        raise RuntimeError("run identity does not match manifest")
    expected = _sha256_bytes(_canonical_bytes(manifest["config"]))
    if expected != manifest.get("config_sha256"):
        raise RuntimeError("manifest config hash mismatch")
    if manifest["config"] != _expected_config():
        raise RuntimeError("runner protocol differs from the frozen run config")
    if require_schema:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (manifest["schema"],)
            ).fetchone()
        if not exists:
            raise RuntimeError("formal benchmark schema is missing; refusing public-schema fallback")
        for required in ("environment.json", "dataset.json"):
            if not (run_dir / required).exists():
                raise RuntimeError(f"initialized run is missing {required}")
    return RunContext(run_id, run_dir, dsn, container, manifest)


def _schema_dsn(dsn: str, schema: str) -> str:
    from psycopg.conninfo import make_conninfo
    return make_conninfo(dsn, options=f"-c search_path={schema},public")


def _connect(ctx: RunContext, *, autocommit: bool = True):
    import psycopg
    from psycopg.rows import dict_row
    from pgvector.psycopg import register_vector
    connection = psycopg.connect(_schema_dsn(ctx.dsn, ctx.schema), autocommit=autocommit,
                                 row_factory=dict_row)
    register_vector(connection)
    return connection


def _db_sizes(connection: Any, schema: str) -> dict[str, Any]:
    database = connection.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()
    rows = connection.execute(
        "SELECT c.relname,pg_relation_size(c.oid) AS table_bytes,pg_indexes_size(c.oid) AS index_bytes,"
        "pg_total_relation_size(c.oid) AS total_bytes,"
        "CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname", (schema,)
    ).fetchall()
    relations = [dict(row) for row in rows]
    return {"database_bytes": int(database["bytes"]), "relations": relations,
            "schema_total_bytes": sum(int(row["total_bytes"]) for row in relations),
            "schema_table_bytes": sum(int(row["table_bytes"]) for row in relations),
            "schema_index_bytes": sum(int(row["index_bytes"]) for row in relations),
            "schema_toast_bytes": sum(int(row["toast_bytes"]) for row in relations)}


def _environment(connection: Any, ctx: RunContext) -> dict[str, Any]:
    settings = {}
    for name in ("shared_buffers", "work_mem", "effective_cache_size", "max_connections",
                 "track_io_timing"):
        settings[name] = connection.execute(f"SHOW {name}").fetchone()[name]
    postgres = connection.execute("SHOW server_version").fetchone()["server_version"]
    pgvector = connection.execute(
        "SELECT extversion FROM pg_extension WHERE extname='vector'"
    ).fetchone()["extversion"]
    disk = os.statvfs(str(ctx.run_dir)) if hasattr(os, "statvfs") else None
    return {
        "schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "captured_at": _utc(),
        "os": platform.platform(), "python": platform.python_version(),
        "cpu_count": os.cpu_count(), "host_memory": _host_memory(),
        "disk_free_bytes": int(disk.f_bavail * disk.f_frsize) if disk else None,
        "postgres": {"version": postgres, "settings": settings},
        "pgvector": {"server_extension_version": pgvector},
        "docker_version": _run_command(["docker", "version", "--format",
                                         "client={{.Client.Version}} server={{.Server.Version}}"]),
        "container": _run_command(["docker", "inspect", ctx.container, "--format",
                                    "{{json .Config.Image}}|{{.Id}}|{{json .Mounts}}"]),
        "stop_guardrails": {"disk_free_bytes_min": 2_000_000_000,
                            "host_available_bytes_min": 1_000_000_000,
                            "sustained_errors": 3},
        "dsn_recorded": False,
    }


def _ensure_main_count(ctx: RunContext, expected: int = DATASET_SIZE) -> dict[str, int]:
    with _connect(ctx) as connection:
        row = connection.execute(
            "SELECT count(*) AS memories,count(v.*) AS vectors "
            "FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.id "
            "WHERE m.namespace=%s", (MAIN_NAMESPACE,)
        ).fetchone()
    result = {"memories": int(row["memories"]), "vectors": int(row["vectors"])}
    if result != {"memories": expected, "vectors": expected}:
        raise RuntimeError(f"main dataset invariant failed: {result}, expected {expected}")
    return result


def init_run(run_id: str, reports_root: Path, dsn: str, container: str) -> RunContext:
    import psycopg
    from psycopg import sql

    run_dir = reports_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(run_dir)
    if path.exists():
        existing_ctx = _load_context(run_id, reports_root, dsn, container, require_schema=False)
        if existing_ctx.manifest.get("initialization_status") == "PASS":
            return _load_context(run_id, reports_root, dsn, container)
        # Backward-compatible reconciliation for a run initialized by an
        # earlier version before initialization_status was introduced.
        if ("applied_migrations" in existing_ctx.manifest and
                (run_dir / "environment.json").exists() and (run_dir / "dataset.json").exists()):
            with psycopg.connect(dsn, autocommit=True) as admin:
                exists = admin.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname=%s", (existing_ctx.schema,)
                ).fetchone()
            if exists:
                existing_ctx.manifest["initialization_status"] = "PASS"
                _save_manifest(existing_ctx)
                return _load_context(run_id, reports_root, dsn, container)
        manifest = existing_ctx.manifest
        ctx = existing_ctx
        schema = ctx.schema
    else:
        schema = _schema_name(run_id)
        config = _expected_config()
        manifest = _new_manifest(run_id, schema, config)
        manifest["initialization_status"] = "RUNNING"
        ctx = RunContext(run_id, run_dir, dsn, container, manifest)
        _save_manifest(ctx)
    with psycopg.connect(dsn, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
        ).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        migration_root = Path(__file__).parents[2] / "migrations"
        with psycopg.connect(_schema_dsn(dsn, schema), autocommit=False) as migration:
            applied = apply_migrations(migration, migration_root)
        from .postgres_store import PostgresStore
        store = PostgresStore(_schema_dsn(dsn, schema), embedder=BenchmarkEmbeddingProvider())
        store.close()
        with _connect(ctx) as connection:
            environment = _environment(connection, ctx)
            database_before = _db_sizes(connection, schema)
        dataset = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id, "name": "postgres-100k-synthetic-v1",
            "sample_count": DATASET_SIZE, "seed": SEED, "namespace_count": 1,
            "user_count": 100, "embedding": {"provider": "postgres-benchmark-fixture",
                "model": "benchmark-hash-v1", "revision": "v1", "dimensions": 96,
                "dtype": "float32-compatible", "synthetic": True,
                "semantic_quality_claim": False},
            "metadata_distribution": {"cohort_1_percent": 1000, "cohort_10_percent": 10000,
                                      "cohort_50_percent": 50000},
            "temporal_distribution": {"span_days": 1825, "closed_window_fraction": 0.2},
            "payload": {"type": "deterministic synthetic text/jsonb", "real_data": False},
            "limitations": ["synthetic vector infrastructure benchmark",
                "does not measure embedding quality, semantic memory accuracy, or real-world recall"],
        }
        manifest["applied_migrations"] = applied
        manifest["database_size_before"] = database_before
        manifest["initialization_status"] = "PASS"
        _write_json(run_dir / "environment.json", environment)
        _write_json(run_dir / "dataset.json", dataset)
        _save_manifest(ctx)
        return ctx
    except Exception:
        # Preserve the formal schema for forensic inspection and safe resume.
        manifest["status"] = "FAILED"
        manifest["initialization_error"] = traceback.format_exc()
        _save_manifest(ctx)
        raise


def _checkpoint(ctx: RunContext, key: str, function: Callable[[RunContext], dict[str, Any]]) -> dict[str, Any]:
    entry = ctx.manifest["checkpoints"][key]
    artifact_path = ctx.run_dir / CHECKPOINTS[key]
    if entry.get("status") == "PASS":
        if not artifact_path.exists() or _file_sha256(artifact_path) != entry.get("sha256"):
            raise RuntimeError(f"completed checkpoint {key} artifact hash mismatch")
        _ensure_main_count(ctx)
        return _read_json(artifact_path)
    for prerequisite in PREREQUISITES[key]:
        if ctx.manifest["checkpoints"][prerequisite]["status"] != "PASS":
            raise RuntimeError(f"checkpoint {key} requires PASS checkpoint {prerequisite}")
    entry["status"] = "RUNNING"
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["started_at"] = _utc()
    ctx.manifest["current_checkpoint"] = key
    _save_manifest(ctx)
    started = _utc()
    try:
        body = function(ctx)
        artifact = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id,
                    "checkpoint": key, "status": "COMPLETED", "started_at": started,
                    "finished_at": _utc(), "last_successful_checkpoint":
                    ctx.manifest.get("last_successful_checkpoint"), **body}
        digest = _write_json(artifact_path, artifact)
        reread = _read_json(artifact_path)
        if reread.get("status") != "COMPLETED":
            raise RuntimeError("artifact validation failed")
        _ensure_main_count(ctx)
        entry.update({"status": "PASS", "finished_at": artifact["finished_at"],
                      "sha256": digest, "error": None})
        ctx.manifest["last_successful_checkpoint"] = key
        ctx.manifest["current_checkpoint"] = None
        ctx.manifest["status"] = "PARTIAL"
        _save_manifest(ctx)
        return artifact
    except Exception as exc:
        failed = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id,
                  "checkpoint": key, "status": "FAILED", "started_at": started,
                  "finished_at": _utc(), "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc(), "last_successful_checkpoint":
                  ctx.manifest.get("last_successful_checkpoint")}
        if isinstance(exc, CheckpointFailure):
            failed.update(exc.evidence)
        digest = _write_json(artifact_path, failed)
        entry.update({"status": "FAILED", "finished_at": failed["finished_at"],
                      "sha256": digest, "error": failed["error"]})
        ctx.manifest["status"] = "FAILED"
        ctx.manifest["current_checkpoint"] = None
        _save_manifest(ctx)
        raise


def _vector_text(text: str) -> str:
    return "[" + ",".join(f"{value:.9g}" for value in stable_vector(text, dimensions=96)) + "]"


def _bulk_insert(connection: Any, namespace: str, count: int, seed: int) -> dict[str, Any]:
    generation = connection.execute(
        "SELECT active_generation FROM embedding_state WHERE id=true"
    ).fetchone()["active_generation"]
    connection.execute(
        "CREATE TEMP TABLE benchmark_stage(memory_id text,content text,structured_content jsonb,"
        "valid_from timestamptz,valid_to timestamptz,observed_at timestamptz,embedding vector(96)) "
        "ON COMMIT DROP"
    )
    base = datetime(2021, 1, 1, tzinfo=timezone.utc)
    generated = perf_counter()
    with connection.cursor().copy(
        "COPY benchmark_stage(memory_id,content,structured_content,valid_from,valid_to,observed_at,embedding) "
        "FROM STDIN"
    ) as copy:
        for index in range(count):
            content = f"benchmark memory {index:06d} user {index % 100:03d} seed {seed}"
            valid_from = base + timedelta(days=index % 1825)
            valid_to = valid_from + timedelta(days=365) if index % 5 == 0 else None
            metadata = {"user_id": f"user-{index % 100:03d}", "ordinal": index,
                        "cohort_1": index < max(1, count // 100),
                        "cohort_10": index < max(1, count // 10),
                        "cohort_50": index < max(1, count // 2),
                        "seed": seed}
            copy.write_row((f"mem_bench_{index:06d}", content,
                            json.dumps(metadata, separators=(",", ":")), valid_from, valid_to,
                            valid_from, _vector_text(content)))
    generation_seconds = perf_counter() - generated
    loading = perf_counter()
    connection.execute(
        "INSERT INTO memories(id,namespace,kind,evidence_state,content,structured_content,status,importance,"
        "confidence,salience,durability,valid_window,observed_at,model_version,extractor_version) "
        "SELECT memory_id,%s,'semantic_fact','FACT',content,structured_content,'ACTIVE',0.5,0.9,0.5,"
        "'long_term',tstzrange(valid_from,valid_to,'[)'),observed_at,'benchmark-hash-v1','bulk-v1' "
        "FROM benchmark_stage", (namespace,)
    )
    connection.execute(
        "INSERT INTO memory_vectors(memory_id,generation_id,embedding,provider,model,revision,dimensions,degraded) "
        "SELECT memory_id,%s,embedding,'postgres-benchmark-fixture','benchmark-hash-v1','v1',96,false "
        "FROM benchmark_stage", (generation,)
    )
    connection.execute("ANALYZE memories")
    connection.execute("ANALYZE memory_vectors")
    load_seconds = perf_counter() - loading
    return {"dataset_generation_seconds": generation_seconds, "bulk_insert_seconds": load_seconds,
            "total_seconds": generation_seconds + load_seconds,
            "rows_per_second": count / max(load_seconds, 1e-9), "generation_id": generation}


def smoke_test(ctx: RunContext) -> dict[str, Any]:
    import psycopg
    from psycopg import sql
    from .postgres_store import PostgresStore
    from .service import MemoryService
    from .retrieval import RetrievalConfig

    smoke_schema = f"smoke_{hashlib.sha256((ctx.run_id + _utc()).encode()).hexdigest()[:16]}"
    started = _utc()
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id,
                              "status": "FAILED", "started_at": started,
                              "formal_performance_claim": False, "row_count": 1000}
    store = None
    try:
        with psycopg.connect(ctx.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(smoke_schema)))
        with psycopg.connect(_schema_dsn(ctx.dsn, smoke_schema), autocommit=False) as migration:
            apply_migrations(migration, Path(__file__).parents[2] / "migrations")
        smoke_ctx = RunContext(ctx.run_id, ctx.run_dir, ctx.dsn, ctx.container,
                               {"schema": smoke_schema})
        store = PostgresStore(_schema_dsn(ctx.dsn, smoke_schema), embedder=BenchmarkEmbeddingProvider())
        store.close()
        store = None
        with _connect(smoke_ctx, autocommit=False) as connection:
            bulk = _bulk_insert(connection, "smoke-main", 1000, SEED)
            connection.commit()
        store = PostgresStore(_schema_dsn(ctx.dsn, smoke_schema), embedder=BenchmarkEmbeddingProvider())
        service = MemoryService(store=store)
        dense = RetrievalConfig(frozenset({"dense"}), candidate_depths={"dense": 100}, name="smoke")
        retrieved = service.retrieve("smoke-main", "benchmark memory 000010 user 010 seed 20260920",
                                     limit=5, config=dense)
        written = service.ingest("smoke-write", "请记住 smoke checkpoint fact", explicit=True,
                                 idempotency_key="smoke-write-1")
        memory_id = written["memory_ids"][0]
        service.forget(memory_id, hard=True)
        hard_deleted = service.get_memory(memory_id) is None
        filtered = store.connection.execute(
            "SELECT count(*) AS count FROM memories WHERE namespace='smoke-main' "
            "AND structured_content->>'cohort_10'='true'"
        ).fetchone()["count"]
        result.update({"status": "COMPLETED", "finished_at": _utc(), "bulk": bulk,
                       "retrieval_nonempty": bool(retrieved.items),
                       "filtered_candidate_count": int(filtered), "hard_delete_verified": hard_deleted,
                       "resume_manifest_readable": _read_json(_manifest_path(ctx.run_dir))["run_id"] == ctx.run_id,
                       "limitations": ["smoke test only; no performance conclusion"]})
        _write_json(ctx.run_dir / "smoke-test.json", result)
        ctx.manifest["smoke_test"] = "PASS"
        ctx.manifest["smoke_test_artifact"] = "smoke-test.json"
        _save_manifest(ctx)
        return result
    except Exception as exc:
        result.update({"finished_at": _utc(), "error": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc()})
        _write_json(ctx.run_dir / "smoke-test.json", result)
        ctx.manifest["smoke_test"] = "FAILED"
        _save_manifest(ctx)
        raise
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(ctx.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(smoke_schema)))


def checkpoint_a(ctx: RunContext) -> dict[str, Any]:
    if ctx.manifest.get("smoke_test") != "PASS":
        raise RuntimeError("formal bulk load requires a passing smoke test")
    resources_before = _resource_snapshot(ctx.container)
    with _connect(ctx, autocommit=False) as connection:
        existing = connection.execute(
            "SELECT count(*) AS count FROM memories WHERE namespace=%s", (MAIN_NAMESPACE,)
        ).fetchone()["count"]
        if existing not in (0, DATASET_SIZE):
            raise RuntimeError(f"partial main dataset is unsafe to overwrite: {existing}")
        sizes_before = _db_sizes(connection, ctx.schema)
        if existing == DATASET_SIZE:
            bulk = {"resume_reconciled": True, "note": "existing committed 100K dataset reused"}
        else:
            bulk = _bulk_insert(connection, MAIN_NAMESPACE, DATASET_SIZE, SEED)
            connection.commit()
        row = connection.execute(
            "SELECT count(*) AS memories,count(v.*) AS vectors,min(vector_dims(v.embedding)) AS min_dims,"
            "max(vector_dims(v.embedding)) AS max_dims,count(*) FILTER (WHERE m.content IS NULL OR m.content='') "
            "AS empty_content FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.id "
            "WHERE m.namespace=%s", (MAIN_NAMESPACE,)
        ).fetchone()
        integrity = {key: int(row[key]) for key in row}
        if integrity != {"memories": DATASET_SIZE, "vectors": DATASET_SIZE,
                         "min_dims": 96, "max_dims": 96, "empty_content": 0}:
            raise RuntimeError(f"bulk integrity failure: {integrity}")
        sizes_after = _db_sizes(connection, ctx.schema)
        server_version = connection.execute("SHOW server_version").fetchone()["server_version"]
        pgvector_version = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()["extversion"]
    return {"scope": "100K synthetic vector infrastructure benchmark", "bulk": bulk,
            "transaction_strategy": "single transaction via temporary COPY staging table",
            "batch_size": DATASET_SIZE, "loaded_tables": ["memories", "memory_vectors"],
            "row_count_verification": integrity, "database_size_before": sizes_before,
            "database_size_after": sizes_after,
            "size_delta_bytes": sizes_after["schema_total_bytes"] - sizes_before["schema_total_bytes"],
            "postgres_version": server_version, "pgvector_version": pgvector_version,
            "resources": {"before": resources_before, "after": _resource_snapshot(ctx.container)},
            "limitations": ["bulk COPY is not production ingest throughput",
                "bulk fixture omits service-path event/provenance projections",
                "96-dimensional synthetic vectors; no semantic quality claim"]}


def _explain(connection: Any, query: str, params: tuple[Any, ...]) -> Any:
    row = connection.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params).fetchone()
    return next(iter(row.values()))


def _raw_exact_query(connection: Any, query_vector: Any, *, where: str = "",
                     extra_params: tuple[Any, ...] = (), limit: int = 5) -> list[dict[str, Any]]:
    sql_text = (
        "SELECT m.id,m.content,1.0-(v.embedding <=> %s) AS cosine_similarity "
        "FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
        "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') " + where +
        " ORDER BY v.embedding <=> %s,m.id LIMIT %s"
    )
    return list(connection.execute(sql_text,
        (query_vector, MAIN_NAMESPACE, *extra_params, query_vector, limit)).fetchall())


def checkpoint_b(ctx: RunContext) -> dict[str, Any]:
    from pgvector import Vector
    from .postgres_store import PostgresStore
    from .service import MemoryService
    from .retrieval import RetrievalConfig

    _ensure_main_count(ctx)
    rng = random.Random(SEED + 1)
    targets = [rng.randrange(DATASET_SIZE) for _ in range(100)]
    dense = RetrievalConfig(frozenset({"dense"}), candidate_depths={"dense": 100},
                            name="postgres-exact-dense")
    first_store = PostgresStore(_schema_dsn(ctx.dsn, ctx.schema), embedder=BenchmarkEmbeddingProvider())
    try:
        first_service = MemoryService(store=first_store)
        target = targets[0]
        start = perf_counter()
        first = first_service.retrieve(MAIN_NAMESPACE,
            f"benchmark memory {target:06d} user {target % 100:03d} seed {SEED}", limit=5, config=dense)
        first_ms = (perf_counter() - start) * 1000
        first_correct = bool(first.items and first.items[0].memory.id == f"mem_bench_{target:06d}")
    finally:
        first_store.close()
    store = PostgresStore(_schema_dsn(ctx.dsn, ctx.schema), embedder=BenchmarkEmbeddingProvider())
    service = MemoryService(store=store)
    try:
        warm_target = targets[0]
        service.retrieve(MAIN_NAMESPACE,
            f"benchmark memory {warm_target:06d} user {warm_target % 100:03d} seed {SEED}",
            limit=5, config=dense)
        latencies: list[float] = []
        hits = 0
        resources_before = _resource_snapshot(ctx.container)
        for target in targets:
            query = f"benchmark memory {target:06d} user {target % 100:03d} seed {SEED}"
            start = perf_counter()
            result = service.retrieve(MAIN_NAMESPACE, query, limit=5, config=dense)
            latencies.append((perf_counter() - start) * 1000)
            hits += int(bool(result.items and result.items[0].memory.id == f"mem_bench_{target:06d}"))
        resources_after = _resource_snapshot(ctx.container)
    finally:
        store.close()
    with _connect(ctx) as connection:
        content = f"benchmark memory {targets[-1]:06d} user {targets[-1] % 100:03d} seed {SEED}"
        vector = Vector(stable_vector(content, dimensions=96))
        plan_query = (
            "SELECT m.id FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
            "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') "
            "ORDER BY v.embedding <=> %s,m.id LIMIT 100"
        )
        plan = _explain(connection, plan_query, (MAIN_NAMESPACE, vector))
        ann_indexes = list(connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s "
            "AND (lower(indexdef) LIKE '%%hnsw%%' OR lower(indexdef) LIKE '%%ivfflat%%')",
            (ctx.schema,)).fetchall())
    if ann_indexes:
        raise RuntimeError(f"ANN index exists before exact baseline: {ann_indexes}")
    stats = _summary(latencies)
    return {"protocol": {"path": "MemoryService.retrieve", "search": "exact cosine",
                         "candidate_depth": 100, "limit": 5, "warmup_count": 1,
                         "measured_sample_count": len(latencies),
                         "cache_terms": ["fresh-connection first-query proxy", "warm repeated-query"],
                         "true_cold_cache_claim": False},
            "first_query_proxy": {"latency_ms": first_ms, "top1_correct": first_correct},
            "warm_query_latency_ms": stats, "raw_latency_ms": latencies,
            "throughput_queries_per_second": 1000.0 / max(float(stats["mean"]), 1e-9),
            "exact_fixture_top1_rate": hits / len(targets), "query_plan": plan,
            "ann_index_count": 0, "resources": {"before": resources_before, "after": resources_after},
            "comparison_with_10k": {"baseline_p50_ms": 167.1783, "baseline_p95_ms": 176.7786,
                "baseline_p99_ms": 179.3637, "protocol": "partially comparable",
                "caution": "same service retrieval shape but 100K bulk fixture omits 10K service projections"},
            "limitations": ["exact-string synthetic fixture is plumbing correctness, not semantic recall",
                            "fresh connection is not a true cold-cache benchmark"]}


def _timed_raw_queries(connection: Any, targets: list[int], where: str,
                       extra_params: tuple[Any, ...]) -> tuple[list[float], int]:
    from pgvector import Vector
    latencies: list[float] = []
    hits = 0
    for target in targets:
        content = f"benchmark memory {target:06d} user {target % 100:03d} seed {SEED}"
        vector = Vector(stable_vector(content, dimensions=96))
        start = perf_counter()
        rows = _raw_exact_query(connection, vector, where=where, extra_params=extra_params)
        latencies.append((perf_counter() - start) * 1000)
        hits += int(bool(rows and rows[0]["id"] == f"mem_bench_{target:06d}"))
    return latencies, hits


def checkpoint_c(ctx: RunContext) -> dict[str, Any]:
    from pgvector import Vector
    rng = random.Random(SEED + 2)
    treatments = {
        "high_selectivity_1_percent": ("AND m.structured_content->>'cohort_1'='true'", 1000),
        "medium_selectivity_10_percent": ("AND m.structured_content->>'cohort_10'='true'", 10000),
        "low_selectivity_50_percent": ("AND m.structured_content->>'cohort_50'='true'", 50000),
    }
    results: dict[str, Any] = {}
    resources_before = _resource_snapshot(ctx.container)
    with _connect(ctx) as connection:
        for name, (where, expected) in treatments.items():
            candidates = int(connection.execute(
                "SELECT count(*) AS count FROM memories m WHERE m.namespace=%s " + where,
                (MAIN_NAMESPACE,)).fetchone()["count"])
            if candidates != expected:
                raise RuntimeError(f"{name} candidate mismatch: {candidates} != {expected}")
            targets = [rng.randrange(expected) for _ in range(100)]
            warm_content = f"benchmark memory {targets[0]:06d} user {targets[0] % 100:03d} seed {SEED}"
            warm_vector = Vector(stable_vector(warm_content, dimensions=96))
            _raw_exact_query(connection, warm_vector, where=where)
            latencies, hits = _timed_raw_queries(connection, targets, where, ())
            plan_sql = (
                "SELECT m.id FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
                "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') " + where +
                " ORDER BY v.embedding <=> %s,m.id LIMIT 5"
            )
            results[name] = {"candidate_count": candidates,
                "actual_selectivity": candidates / DATASET_SIZE, "expected_candidate_count": expected,
                "latency_ms": _summary(latencies), "raw_latency_ms": latencies,
                "top1_correct_rate": hits / len(targets),
                "query_plan": _explain(connection, plan_sql, (MAIN_NAMESPACE, warm_vector))}
    return {"protocol": {"path": "direct parameterized SQL infrastructure path",
                         "service_api_equivalent": False,
                         "reason": "PostgresStore predicate channel is ranking, not SQL metadata filtering",
                         "warmup_count_per_treatment": 1, "measured_queries_per_treatment": 100},
            "treatments": results,
            "resources": {"before": resources_before, "after": _resource_snapshot(ctx.container)},
            "limitations": ["JSON metadata predicates have no dedicated index in the current schema",
                            "does not claim service-level filtered retrieval parity"]}


def checkpoint_d(ctx: RunContext) -> dict[str, Any]:
    from pgvector import Vector
    from .postgres_store import PostgresStore
    from .service import MemoryService
    from .retrieval import RetrievalConfig
    rng = random.Random(SEED + 3)
    current_candidates = [index for index in range(DATASET_SIZE) if index % 5 != 0]
    historical_candidates = [index for index in range(DATASET_SIZE) if index % 1825 <= 100]
    window_candidates = [index for index in range(DATASET_SIZE)
                         if index % 1825 < 455 and not (index % 5 == 0 and index % 1825 == 0)]
    definitions = {
        "current_fact": ("AND m.valid_window @> %s::timestamptz", ("2030-01-01T00:00:00+00:00",), current_candidates),
        "historical_fact": ("AND m.valid_window @> %s::timestamptz", ("2021-04-11T00:00:00+00:00",), historical_candidates),
        "window_overlap": ("AND m.valid_window && tstzrange(%s,%s,'[)')",
                           ("2022-01-01T00:00:00+00:00", "2022-04-01T00:00:00+00:00"), window_candidates),
    }
    results: dict[str, Any] = {}
    with _connect(ctx) as connection:
        for name, (where, params, eligible) in definitions.items():
            targets = [eligible[rng.randrange(len(eligible))] for _ in range(100)]
            first = targets[0]
            content = f"benchmark memory {first:06d} user {first % 100:03d} seed {SEED}"
            vector = Vector(stable_vector(content, dimensions=96))
            _raw_exact_query(connection, vector, where=where, extra_params=params)
            latencies, hits = _timed_raw_queries(connection, targets, where, params)
            plan_sql = (
                "SELECT m.id FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
                "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') " + where +
                " ORDER BY v.embedding <=> %s,m.id LIMIT 5"
            )
            plan_params = (MAIN_NAMESPACE, *params, vector)
            results[name] = {"eligible_fixture_count": len(eligible), "latency_ms": _summary(latencies),
                             "raw_latency_ms": latencies, "correct_result_rate": hits / len(targets),
                             "query_plan": _explain(connection, plan_sql, plan_params)}
    fixture_namespace = f"temporal-fixture-{ctx.run_id}"
    _reset_fixture_namespace(ctx, fixture_namespace)
    store = PostgresStore(_schema_dsn(ctx.dsn, ctx.schema), embedder=BenchmarkEmbeddingProvider())
    try:
        service = MemoryService(store=store)
        old = service.ingest(fixture_namespace, "请记住我住在成都", explicit=True,
                             observed_at="2025-01-01T00:00:00+00:00")
        new = service.ingest(fixture_namespace, "请记住我住在上海", explicit=True,
                             observed_at="2026-01-01T00:00:00+00:00")
        dense_temporal = RetrievalConfig(frozenset({"dense", "temporal"}),
                                         candidate_depths={"dense": 100}, name="temporal-fixture")
        current = service.retrieve(fixture_namespace, "我现在住在哪里", limit=5, config=dense_temporal)
        historical = service.retrieve(fixture_namespace, "2025年我住在哪里", limit=5,
                                      as_of="2025-06-01T00:00:00+00:00", config=dense_temporal)
        correctness = {"old_memory_id": old["memory_ids"][0], "new_memory_id": new["memory_ids"][0],
                       "current_contains_new": new["memory_ids"][0] in [i.memory.id for i in current.items],
                       "historical_contains_old": old["memory_ids"][0] in [i.memory.id for i in historical.items]}
    finally:
        store.close()
    if not all((correctness["current_contains_new"], correctness["historical_contains_old"])):
        raise RuntimeError(f"temporal service correctness failed: {correctness}")
    return {"protocol": {"performance_path": "direct SQL with valid_window predicates",
                         "correctness_path": "MemoryService temporal fixture",
                         "service_limitation": "temporal visibility is applied after dense Top-100 in Python"},
            "treatments": results, "service_correctness": correctness,
            "limitations": ["SQL temporal performance and service correctness are separate evidence",
                            "does not claim the service uses GiST before vector candidate selection"]}


def _new_service(ctx: RunContext):
    from .postgres_store import PostgresStore
    from .service import MemoryService
    store = PostgresStore(_schema_dsn(ctx.dsn, ctx.schema), embedder=BenchmarkEmbeddingProvider())
    return MemoryService(store=store)


def _reset_fixture_namespace(ctx: RunContext, namespace: str) -> None:
    """Remove only an incomplete checkpoint's deterministic fixture namespace.

    The 100K main namespace is explicitly forbidden. This makes retries of a
    failed mutable checkpoint deterministic without touching completed evidence.
    """
    if namespace == MAIN_NAMESPACE:
        raise RuntimeError("refusing to reset the main benchmark namespace")
    service = _new_service(ctx)
    try:
        service.store.purge_namespace(namespace)
    finally:
        service.store.close()


def checkpoint_e(ctx: RunContext) -> dict[str, Any]:
    namespace = f"incremental-{ctx.run_id}"
    _reset_fixture_namespace(ctx, namespace)
    resources_before = _resource_snapshot(ctx.container)
    service = _new_service(ctx)
    single_latencies: list[float] = []
    single_failures = 0
    try:
        for index in range(20):
            started = perf_counter()
            try:
                result = service.ingest(namespace, f"请记住 incremental single fact {index}",
                                        explicit=True, idempotency_key=f"single-{index}")
                if not result["memory_ids"]:
                    raise RuntimeError("single ingest produced no memory")
            except Exception:
                single_failures += 1
                raise
            finally:
                single_latencies.append((perf_counter() - started) * 1000)

        batch_latencies: list[float] = []
        for batch in range(10):
            started = perf_counter()
            with service.store.transaction():
                for item in range(5):
                    service.ingest(namespace, f"请记住 incremental batch {batch} item {item}",
                                   explicit=True, idempotency_key=f"batch-{batch}-{item}")
            batch_latencies.append((perf_counter() - started) * 1000)

        idem_first = service.ingest(namespace, "请记住 idempotency fixture", explicit=True,
                                    idempotency_key="idempotency-shared")
        idem_second = service.ingest(namespace, "请记住 idempotency fixture", explicit=True,
                                     idempotency_key="idempotency-shared")
    finally:
        service.store.close()

    def writer(worker: int) -> dict[str, Any]:
        worker_service = _new_service(ctx)
        latencies: list[float] = []
        failures: list[str] = []
        try:
            for item in range(10):
                started = perf_counter()
                try:
                    worker_service.ingest(
                        namespace, f"请记住 concurrent writer {worker} fact {item}", explicit=True,
                        idempotency_key=f"writer-{worker}-{item}")
                except Exception as exc:  # pragma: no cover - recorded live failure
                    failures.append(f"{type(exc).__name__}: {exc}")
                latencies.append((perf_counter() - started) * 1000)
        finally:
            worker_service.store.close()
        return {"latencies": latencies, "failures": failures}

    concurrent_started = perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        writer_results = list(executor.map(writer, range(4)))
    concurrent_seconds = perf_counter() - concurrent_started
    concurrent_latencies = [value for result in writer_results for value in result["latencies"]]
    concurrent_failures = [error for result in writer_results for error in result["failures"]]
    with _connect(ctx) as connection:
        counts = connection.execute(
            "SELECT count(*) AS memories,count(DISTINCT e.id) AS events,"
            "count(DISTINCT e.idempotency_key) FILTER (WHERE e.idempotency_key IS NOT NULL) AS keys "
            "FROM events e LEFT JOIN memory_sources s ON s.event_id=e.id "
            "LEFT JOIN memories m ON m.id=s.memory_id WHERE e.namespace=%s", (namespace,)
        ).fetchone()
    idempotency_ok = (idem_first["event_id"] == idem_second["event_id"] and
                      idem_first["memory_ids"] == idem_second["memory_ids"] and
                      idem_second["accepted"] is False)
    if concurrent_failures or not idempotency_ok:
        raise RuntimeError(f"incremental correctness failure: errors={concurrent_failures}, "
                           f"idempotency={idempotency_ok}")
    single_stats = _summary(single_latencies)
    batch_item_ms = [latency / 5.0 for latency in batch_latencies]
    return {
        "protocol": {"path": "MemoryService.ingest production service path",
                     "bulk_copy_included": False, "namespace": namespace},
        "single_request": {"operation_count": 20, "latency_ms": single_stats,
                           "memories_per_second": 1000.0 / max(float(single_stats["mean"]), 1e-9),
                           "transaction_failures": single_failures, "retry_count": 0},
        "small_batch": {"batch_count": 10, "items_per_batch": 5,
                        "batch_latency_ms": _summary(batch_latencies),
                        "per_item_latency_ms": _summary(batch_item_ms),
                        "transaction_strategy": "five service ingests in one outer transaction"},
        "concurrent_writers": {"workers": 4, "operation_count": len(concurrent_latencies),
                               "duration_seconds": concurrent_seconds,
                               "memories_per_second": len(concurrent_latencies) / concurrent_seconds,
                               "latency_ms": _summary(concurrent_latencies),
                               "transaction_failures": len(concurrent_failures),
                               "errors": concurrent_failures, "retry_count": 0},
        "idempotency": {"correct": idempotency_ok, "first_accepted": idem_first["accepted"],
                        "second_accepted": idem_second["accepted"],
                        "same_event": idem_first["event_id"] == idem_second["event_id"]},
        "database_counts": {key: int(value) for key, value in counts.items()},
        "resources": {"before": resources_before, "after": _resource_snapshot(ctx.container)},
        "limitations": ["fixture uses deterministic synthetic embeddings",
                        "small batch is an outer transaction over service calls, not a native batch API"]}


def checkpoint_f(ctx: RunContext) -> dict[str, Any]:
    namespace = f"deletion-fixture-{ctx.run_id}"
    _reset_fixture_namespace(ctx, namespace)
    service = _new_service(ctx)
    try:
        soft = service.ingest(namespace, "请记住 deletion soft fixture", explicit=True,
                              idempotency_key="delete-soft")
        hard = service.ingest(namespace, "请记住 deletion hard fixture", explicit=True,
                              idempotency_key="delete-hard")
        soft_id = soft["memory_ids"][0]
        hard_id = hard["memory_ids"][0]
        soft_started = perf_counter()
        service.forget(soft_id, reason="benchmark_soft_delete", hard=False)
        soft_ms = (perf_counter() - soft_started) * 1000
        hard_started = perf_counter()
        service.forget(hard_id, reason="benchmark_hard_purge", hard=True)
        hard_ms = (perf_counter() - hard_started) * 1000
        with service.store.connection.cursor() as cursor:
            soft_row = cursor.execute(
                "SELECT status FROM memories WHERE id=%s", (soft_id,)).fetchone()
            soft_vector = cursor.execute(
                "SELECT count(*) AS count FROM memory_vectors WHERE memory_id=%s", (soft_id,)).fetchone()
            hard_memory = cursor.execute(
                "SELECT count(*) AS count FROM memories WHERE id=%s", (hard_id,)).fetchone()
            hard_residue = {}
            for table, column in (("memory_vectors", "memory_id"), ("memory_sources", "memory_id"),
                                  ("memory_versions", "memory_id"), ("memory_keys", "memory_id"),
                                  ("memory_access", "memory_id"), ("memory_entities", "memory_id"),
                                  ("memory_relations", "memory_id")):
                hard_residue[table] = int(cursor.execute(
                    f"SELECT count(*) AS count FROM {table} WHERE {column}=%s", (hard_id,)
                ).fetchone()["count"])
            hard_residue["memory_transitions"] = int(cursor.execute(
                "SELECT count(*) AS count FROM memory_transitions "
                "WHERE from_memory_id=%s OR to_memory_id=%s", (hard_id, hard_id)
            ).fetchone()["count"])
            tombstones = int(cursor.execute(
                "SELECT count(*) AS count FROM tombstones WHERE object_type='memory' "
                "AND object_id IN (%s,%s)", (soft_id, hard_id)
            ).fetchone()["count"])

        before_rollback = int(service.store.connection.execute(
            "SELECT count(*) AS count FROM memories WHERE namespace=%s", (namespace,)
        ).fetchone()["count"])
        rollback_raised = False
        try:
            with service.store.transaction():
                service.ingest(namespace, "请记住 rollback deletion fixture", explicit=True,
                               idempotency_key="delete-rollback")
                raise RuntimeError("injected rollback")
        except RuntimeError as exc:
            rollback_raised = str(exc) == "injected rollback"
        after_rollback = int(service.store.connection.execute(
            "SELECT count(*) AS count FROM memories WHERE namespace=%s", (namespace,)
        ).fetchone()["count"])
    finally:
        service.store.close()
    correctness = {
        "soft_memory_retained_deleted_status": bool(soft_row and soft_row["status"] == "DELETED"),
        "soft_embedding_removed": int(soft_vector["count"]) == 0,
        "hard_memory_removed": int(hard_memory["count"]) == 0,
        "hard_projection_residue_counts": hard_residue,
        "hard_all_projections_removed": all(value == 0 for value in hard_residue.values()),
        "audit_tombstones_count": tombstones,
        "rollback_raised": rollback_raised,
        "rollback_preserved_row_count": before_rollback == after_rollback,
    }
    if not all((correctness["soft_memory_retained_deleted_status"], correctness["soft_embedding_removed"],
                correctness["hard_memory_removed"], correctness["hard_all_projections_removed"],
                correctness["audit_tombstones_count"] == 2, correctness["rollback_raised"],
                correctness["rollback_preserved_row_count"])):
        raise RuntimeError(f"deletion correctness failed: {correctness}")
    return {"fixture_namespace": namespace, "main_dataset_mutated": False,
            "soft_delete_latency_ms": soft_ms, "hard_purge_latency_ms": hard_ms,
            "correctness": correctness,
            "audit_behavior": "memory tombstones retained for soft and hard deletion",
            "limitations": ["deletion uses an isolated small fixture namespace",
                            "latency does not represent deleting from the main 100K namespace"]}


def _concurrent_query_worker(ctx: RunContext, targets: list[int]) -> dict[str, Any]:
    from pgvector import Vector
    latencies: list[float] = []
    errors: list[str] = []
    connection = _connect(ctx)
    try:
        for target in targets:
            content = f"benchmark memory {target:06d} user {target % 100:03d} seed {SEED}"
            vector = Vector(stable_vector(content, dimensions=96))
            started = perf_counter()
            try:
                rows = _raw_exact_query(connection, vector)
                if not rows:
                    raise RuntimeError("empty exact result")
            except Exception as exc:  # pragma: no cover - live evidence
                errors.append(f"{type(exc).__name__}: {exc}")
            latencies.append((perf_counter() - started) * 1000)
    finally:
        connection.close()
    return {"latencies": latencies, "errors": errors}


def _skip_locked_check(ctx: RunContext) -> dict[str, Any]:
    from psycopg.types.json import Jsonb
    namespace = f"skip-locked-{ctx.run_id}"
    _reset_fixture_namespace(ctx, namespace)
    event_ids = [f"evt_skip_locked_{ctx.run_id}_{index}" for index in range(8)]
    with _connect(ctx, autocommit=False) as connection:
        for index, event_id in enumerate(event_ids):
            connection.execute(
                "INSERT INTO events(id,namespace,event_type,payload,observed_at,idempotency_key) "
                "VALUES (%s,%s,'benchmark',%s,now(),%s) ON CONFLICT DO NOTHING",
                (event_id, namespace, Jsonb({"fixture": index}), f"skip-{index}"))
            connection.execute(
                "INSERT INTO outbox(event_id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,))
        connection.commit()

    def claim() -> list[str]:
        with _connect(ctx, autocommit=False) as connection:
            rows = connection.execute("SELECT event_id FROM claim_memory_outbox(4,3)").fetchall()
            connection.commit()
            return [row["event_id"] for row in rows if row["event_id"] in event_ids]

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _index: claim(), range(2)))
    flattened = [event_id for group in claimed for event_id in group]
    with _connect(ctx) as connection:
        for event_id in event_ids:
            connection.execute("SELECT complete_memory_outbox(%s)", (event_id,))
    return {"worker_claims": claimed, "claimed_count": len(flattened),
            "disjoint": len(flattened) == len(set(flattened)),
            "all_fixture_events_claimed": set(flattened) == set(event_ids)}


def checkpoint_g(ctx: RunContext) -> dict[str, Any]:
    rng = random.Random(SEED + 4)
    levels: list[dict[str, Any]] = []
    previous_throughput: float | None = None
    saturation_reason = "maximum preregistered worker level completed"
    for workers in (1, 4, 8, 16):
        target_groups = [[rng.randrange(DATASET_SIZE) for _ in range(8)] for _ in range(workers)]
        before = _resource_snapshot(ctx.container)
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda values: _concurrent_query_worker(ctx, values), target_groups))
        elapsed = perf_counter() - started
        latencies = [value for result in results for value in result["latencies"]]
        errors = [error for result in results for error in result["errors"]]
        throughput = len(latencies) / max(elapsed, 1e-9)
        with _connect(ctx) as connection:
            activity = connection.execute(
                "SELECT count(*) AS connections,count(*) FILTER (WHERE wait_event IS NOT NULL) AS waiting "
                "FROM pg_stat_activity WHERE datname=current_database()"
            ).fetchone()
            locks = connection.execute(
                "SELECT count(*) AS count FROM pg_locks WHERE NOT granted"
            ).fetchone()["count"]
        level = {"workers": workers, "operation_count": len(latencies), "duration_seconds": elapsed,
                 "throughput_queries_per_second": throughput, "latency_ms": _summary(latencies),
                 "error_count": len(errors), "errors": errors,
                 "postgres_activity": {"connections": int(activity["connections"]),
                                       "waiting": int(activity["waiting"]),
                                       "ungranted_locks": int(locks)},
                 "resources": {"before": before, "after": _resource_snapshot(ctx.container)}}
        levels.append(level)
        if errors:
            saturation_reason = f"stopped after {workers} workers because errors appeared"
            break
        if previous_throughput is not None and throughput < previous_throughput * 0.8:
            saturation_reason = f"stopped after {workers} workers because throughput fell more than 20%"
            break
        previous_throughput = throughput
    skip_locked = _skip_locked_check(ctx)
    if not skip_locked["disjoint"] or not skip_locked["all_fixture_events_claimed"]:
        raise RuntimeError(f"SKIP LOCKED correctness failed: {skip_locked}")
    evidence = {"protocol": {"queries_per_worker": 8, "connection_model": "one connection per worker",
                             "worker_levels": [level["workers"] for level in levels]},
                "levels": levels, "saturation_stop_reason": saturation_reason,
                "skip_locked_correctness": skip_locked, "deadlocks": 0, "retry_count": 0,
                "limitations": ["short local concurrency treatment",
                                "Docker stats are point snapshots rather than a high-frequency time series"]}
    error_count = sum(int(level["error_count"]) for level in levels)
    if error_count:
        raise CheckpointFailure(
            f"concurrency produced {error_count} sustained errors; experiment stopped", evidence)
    return evidence


def checkpoint_h(ctx: RunContext) -> dict[str, Any]:
    from pgvector import Vector
    namespace = f"mixed-{ctx.run_id}"
    _reset_fixture_namespace(ctx, namespace)
    setup = _new_service(ctx)
    delete_ids: list[str] = []
    try:
        for index in range(10):
            result = setup.ingest(namespace, f"请记住 mixed delete fixture {index}", explicit=True,
                                  idempotency_key=f"mixed-delete-{index}")
            delete_ids.append(result["memory_ids"][0])
    finally:
        setup.store.close()
    operations: list[tuple[str, int]] = ([('retrieve', index) for index in range(70)] +
                                         [('ingest', index) for index in range(20)] +
                                         [('delete', index) for index in range(10)])
    rng = random.Random(SEED + 5)
    rng.shuffle(operations)

    def execute(operation: tuple[str, int]) -> dict[str, Any]:
        kind, index = operation
        started = perf_counter()
        error = None
        correct = False
        try:
            if kind == "retrieve":
                target = (index * 1423) % DATASET_SIZE
                content = f"benchmark memory {target:06d} user {target % 100:03d} seed {SEED}"
                vector = Vector(stable_vector(content, dimensions=96))
                with _connect(ctx) as connection:
                    rows = _raw_exact_query(connection, vector)
                correct = bool(rows and rows[0]["id"] == f"mem_bench_{target:06d}")
            elif kind == "ingest":
                service = _new_service(ctx)
                try:
                    value = service.ingest(namespace, f"请记住 mixed ingest fact {index}", explicit=True,
                                           idempotency_key=f"mixed-ingest-{index}")
                    correct = bool(value["memory_ids"])
                finally:
                    service.store.close()
            else:
                service = _new_service(ctx)
                try:
                    service.forget(delete_ids[index], reason="mixed_workload", hard=(index % 2 == 0))
                    correct = True
                finally:
                    service.store.close()
        except Exception as exc:  # pragma: no cover - live evidence
            error = f"{type(exc).__name__}: {exc}"
        return {"operation": kind, "latency_ms": (perf_counter() - started) * 1000,
                "correct": correct, "error": error}

    resources_before = _resource_snapshot(ctx.container)
    started = perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(execute, operations))
    duration = perf_counter() - started
    resources_after = _resource_snapshot(ctx.container)
    errors = [row for row in rows if row["error"]]
    incorrect = [row for row in rows if not row["correct"]]
    if errors or incorrect:
        raise RuntimeError(f"mixed workload correctness failure: errors={len(errors)}, "
                           f"incorrect={len(incorrect)}")
    by_operation = {}
    for kind in ("retrieve", "ingest", "delete"):
        samples = [float(row["latency_ms"]) for row in rows if row["operation"] == kind]
        by_operation[kind] = {"operation_count": len(samples), "latency_ms": _summary(samples)}
    return {"workload_definition": {"retrieve_percent": 70, "ingest_percent": 20,
                                     "update_delete_percent": 10, "workers": 4,
                                     "random_seed": SEED + 5},
            "duration_seconds": duration, "operation_count": len(rows),
            "throughput_operations_per_second": len(rows) / duration,
            "by_operation": by_operation, "error_rate": 0.0, "correct_result_rate": 1.0,
            "per_operation_rows": rows,
            "resources": {"before": resources_before, "after": resources_after},
            "limitations": ["simple validation workload, not a production traffic model",
                            "per-operation connection setup is included in this mixed workload"]}


def _plan_node_types(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if "Node Type" in value:
            found.append(str(value["Node Type"]))
        for child in value.values():
            found.extend(_plan_node_types(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_plan_node_types(child))
    return found


def _render_report(ctx: RunContext, artifacts: dict[str, dict[str, Any]], sizes: dict[str, Any],
                   summary: dict[str, Any]) -> str:
    b = artifacts["B"]
    c = artifacts["C"]
    d = artifacts["D"]
    e = artifacts["E"]
    f = artifacts["F"]
    g = artifacts["G"]
    h = artifacts["H"]
    exact = b["warm_query_latency_ms"]
    lines = [
        "# PostgreSQL exact 100K benchmark report", "", f"Run ID: `{ctx.run_id}`  ",
        f"Evidence directory: `eval/reports/postgres-100k/{ctx.run_id}/`", "",
        "## Scope", "",
        "This is a PostgreSQL + pgvector **synthetic vector infrastructure benchmark** at 100,000 rows.",
        "It evaluates storage, exact cosine execution, filtering, temporal queries, concurrency and service-path writes.",
        "It does not evaluate embedding quality, semantic memory accuracy, or real-world recall.", "",
        "## Environment", "", "See `environment.json`. PostgreSQL and pgvector versions, settings, host memory, Docker identity and guardrails are recorded there.", "",
        "## Dataset", "", "One namespace contains exactly 100,000 memories with deterministic 96-dimensional synthetic vectors, 100 users, controlled metadata selectivity, and deterministic temporal windows. See `dataset.json`.", "",
        "## Methodology", "", "Each checkpoint ran separately and published a validated JSON artifact before the next began. No ANN index was created. Fresh-connection first-query measurements are proxies, not true cold-cache measurements.", "",
        "## Bulk Load", "", f"Checkpoint A completed with `{artifacts['A']['transaction_strategy']}`. COPY throughput is not production ingest throughput.", "",
        "## Incremental Ingest", "", f"Single-request service ingest: {e['single_request']['memories_per_second']:.2f} memories/s; p50 {e['single_request']['latency_ms']['p50']:.2f} ms, p95 {e['single_request']['latency_ms']['p95']:.2f} ms. Idempotency correctness: {e['idempotency']['correct']}.", "",
        "## Exact Retrieval", "", f"100 measured warm service queries: p50 {exact['p50']:.2f} ms, p95 {exact['p95']:.2f} ms, p99 {exact['p99']:.2f} ms, mean {exact['mean']:.2f} ms. Exact-string fixture Top-1 rate: {b['exact_fixture_top1_rate']:.3f}.", "",
        "## Filtered Retrieval", "",
    ]
    for name, treatment in c["treatments"].items():
        lines.append(f"- {name}: {treatment['candidate_count']} candidates ({treatment['actual_selectivity']:.1%}), p50 {treatment['latency_ms']['p50']:.2f} ms, p95 {treatment['latency_ms']['p95']:.2f} ms.")
    lines += ["", "These are direct SQL infrastructure measurements because the current service predicate channel is a post-selection ranking signal.", "",
              "## Temporal Retrieval", ""]
    for name, treatment in d["treatments"].items():
        lines.append(f"- {name}: correctness {treatment['correct_result_rate']:.3f}, p50 {treatment['latency_ms']['p50']:.2f} ms, p95 {treatment['latency_ms']['p95']:.2f} ms.")
    lines += ["", "Service-level current/historical fixture correctness also passed; SQL performance and service correctness are intentionally separate evidence.", "",
              "## Delete", "", f"Soft delete took {f['soft_delete_latency_ms']:.2f} ms and hard purge took {f['hard_purge_latency_ms']:.2f} ms on an isolated fixture. Projection cleanup, tombstones and rollback were verified.", "",
              "## Concurrency", ""]
    for level in g["levels"]:
        lines.append(f"- {level['workers']} workers: {level['throughput_queries_per_second']:.2f} queries/s, p95 {level['latency_ms']['p95']:.2f} ms, errors {level['error_count']}.")
    lines += ["", f"Stop reason: {g['saturation_stop_reason']}. SKIP LOCKED disjoint-claim correctness passed.", "",
              "## Mixed Workload", "", f"The 70/20/10 read/ingest/delete workload completed {h['operation_count']} operations at {h['throughput_operations_per_second']:.2f} ops/s with zero recorded errors.", "",
              "## Resource Usage", "", "Host RAM, benchmark-client RSS and Docker container snapshots are kept as separately named measurements in checkpoint artifacts and `resource-usage.json`; they are not combined into one memory number.", "",
              "## Database Size", "", f"Run schema total: {sizes['schema_total_bytes']} bytes; tables {sizes['schema_table_bytes']} bytes; indexes {sizes['schema_index_bytes']} bytes; TOAST {sizes['schema_toast_bytes']} bytes. Bytes per main memory is an observed 96-d fixture ratio, not a real-model projection.", "",
              "## Query Plans", "", "Representative `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` plans for exact, all filter selectivities and temporal treatments are in `query-plans.json`.", "",
              "## Comparison With 10K", "", f"The earlier 10K service baseline was p50 167.18 ms, p95 176.78 ms and p99 179.36 ms. The 100K p50 ratio is {float(exact['p50']) / 167.1783:.2f}×. This comparison is **exploratory and not directly comparable**: the 100K corpus was bulk-loaded without the full event/provenance projections used by the 10K service-ingest corpus, and the sample count changed from 20 to 100.", "",
              "## Observed Bottlenecks", "", str(summary["answers"]["10_current_largest_bottleneck"]), "",
              "## Limitations", "", "- Synthetic 96-d vectors; no semantic quality claim.", "- Cache was not controlled at PostgreSQL shared buffers and OS page-cache levels.", "- Resource values are checkpoint snapshots, not continuous profiling.", "- Filter/temporal SQL timings are distinct from current service behavior.", "",
              "## Evidence Level", "", "Executed local benchmark evidence for this run and environment; no extrapolation to 768/1024/1536 dimensions is presented as measured fact.", "",
              "## What This Benchmark Proves", "", "It establishes observed 100K behavior for the current PostgreSQL exact path, service ingest, fixture deletion, local concurrency, and a small mixed workload under the recorded environment.", "",
              "## What This Benchmark Does NOT Prove", "", "It does not prove semantic retrieval quality, production SLOs, true cold-cache behavior, 1M scalability, ANN recall, or that PostgreSQL can or cannot scale in general.", "",
              "## Required Questions", ""]
    for question, answer in summary["answers"].items():
        lines.append(f"- **{question}**: {answer}")
    return "\n".join(lines) + "\n"


def _ann_plan() -> str:
    return """# PostgreSQL ANN experiment plan

Status: PLAN ONLY — do not execute automatically.

## Gate

Run only after the exact 100K artifacts and report are validated. Preserve the exact result set as ground truth. Do not infer that a slow exact scan means PostgreSQL is unsuitable.

## Treatments

- Exact pgvector cosine (ground truth)
- HNSW, with preregistered `m`/`ef_construction` and dev-only `ef_search` tuning
- IVFFlat, with preregistered `lists` and dev-only `probes` tuning

## Frozen protocol

Use identical 10K and 100K corpora, queries, tenant/metadata filters, temporal windows, Top-K and seeds. Separate unfiltered and approximately 1%, 10% and 50% filtered treatments. Tune only on development queries; evaluate once on the held-out set.

## Measurements

- Recall@K against exact Top-K, including filtered recall
- p50/p95/p99/mean latency and throughput
- Index build time and failure modes
- Index, table and total database size
- Host, container, PostgreSQL and client memory as separate measurements
- Incremental write latency, write amplification and delete/update behavior
- Query plans and filter compatibility
- Fresh-connection first-query proxy and warm repeated-query (never call this true cold cache without controlling PostgreSQL and OS caches)

## Decision rule

Adopt an ANN treatment only if it produces a material latency improvement while meeting a preregistered Recall@K floor and acceptable filtered recall, build time, storage and write costs. Otherwise retain exact search. A separate vector database is outside this experiment and requires a measured PostgreSQL threshold failure.

## Stop conditions

Stop on correctness invariant failure, repeated connection/runtime errors, disk/RAM guardrail breach, or unsafe projected runtime. Save a FAILED/PARTIAL artifact and do not delete the exact schema or evidence.
"""


def finalize(ctx: RunContext) -> dict[str, Any]:
    for key in CHECKPOINTS:
        if ctx.manifest["checkpoints"][key]["status"] != "PASS":
            raise RuntimeError(f"cannot finalize: checkpoint {key} is not PASS")
    artifacts = {key: _read_json(ctx.run_dir / filename) for key, filename in CHECKPOINTS.items()}
    _ensure_main_count(ctx)
    with _connect(ctx) as connection:
        sizes = _db_sizes(connection, ctx.schema)
        final_integrity = connection.execute(
            "SELECT count(*) AS memories,count(v.*) AS vectors,"
            "min(vector_dims(v.embedding)) AS min_dimensions,max(vector_dims(v.embedding)) AS max_dimensions "
            "FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.id WHERE m.namespace=%s",
            (MAIN_NAMESPACE,)).fetchone()
        indexes = list(connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname",
            (ctx.schema,)).fetchall())
    if any("hnsw" in row["indexdef"].lower() or "ivfflat" in row["indexdef"].lower()
           for row in indexes):
        raise RuntimeError("ANN index appeared before final exact closure")
    plans = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id,
             "exact": artifacts["B"]["query_plan"],
             "filtered": {key: value["query_plan"] for key, value in artifacts["C"]["treatments"].items()},
             "temporal": {key: value["query_plan"] for key, value in artifacts["D"]["treatments"].items()}}
    _write_json(ctx.run_dir / "query-plans.json", plans)
    _write_json(ctx.run_dir / "database-size.json", {
        "schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "COMPLETED",
        "captured_at": _utc(), **sizes,
        "bytes_per_main_memory": sizes["schema_total_bytes"] / DATASET_SIZE,
        "dimension_basis": 96,
        "calculated_estimates": {str(dim): {"label": "CALCULATED ESTIMATE",
            "vector_payload_scale_only_bytes_per_memory": 4 * dim}
            for dim in (768, 1024, 1536)},
        "limitations": ["database size includes fixture namespaces in the run schema",
                        "dimension estimates are calculations, not benchmark results"]})
    resources = {key: artifact.get("resources", {"available": False})
                 for key, artifact in artifacts.items()}
    _write_json(ctx.run_dir / "resource-usage.json", {
        "schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "COMPLETED",
        "captured_at": _utc(), "checkpoints": resources,
        "naming": ["host_memory", "docker_container", "benchmark_client"],
        "limitations": ["point snapshots; PostgreSQL is measured through its Docker container"]})
    exact = artifacts["B"]["warm_query_latency_ms"]
    filtered = artifacts["C"]["treatments"]
    temporal = artifacts["D"]["treatments"]
    concurrency_levels = artifacts["G"]["levels"]
    plan_nodes = sorted(set(_plan_node_types(plans)))
    exact_ratio = float(exact["p50"]) / 167.1783
    medium_reduction = 1.0 - filtered["medium_selectivity_10_percent"]["actual_selectivity"]
    baseline_exact = float(exact["p50"])
    temporal_p50 = mean(float(value["latency_ms"]["p50"]) for value in temporal.values())
    best_level = max(concurrency_levels, key=lambda row: row["throughput_queries_per_second"])
    answers = {
        "1_10K_to_100K_exact_latency": f"100K p50 {exact['p50']:.2f} ms vs 10K p50 167.18 ms ({exact_ratio:.2f}× exploratory ratio).",
        "2_is_scaling_linear": ("The exploratory p50 ratio is close to the 10× row-count increase."
                                if 7.5 <= exact_ratio <= 12.5 else
                                "The exploratory p50 ratio is not close to the 10× row-count increase."),
        "3_dominant_time_component": "Exact vector distance scan/sort plus service hydration round trips dominate; see exact EXPLAIN and service-path limitation.",
        "4_metadata_filter_workload_reduction": f"The 10% treatment reduced eligible candidates by {medium_reduction:.0%}; measured latencies and plans are in retrieval-filtered.json.",
        "5_temporal_extra_cost": f"Mean temporal-treatment p50 was {temporal_p50:.2f} ms versus exact p50 {baseline_exact:.2f} ms on the separate SQL path; this is not a service-overhead ratio.",
        "6_planner_behavior": f"Observed plan node types: {', '.join(plan_nodes)}. No HNSW/IVFFlat index existed.",
        "7_bulk_vs_incremental": f"Bulk COPY and service ingest use different protocols; service single-request throughput was {artifacts['E']['single_request']['memories_per_second']:.2f}/s and is not directly comparable to COPY.",
        "8_concurrency_first_saturation": f"Highest observed throughput was at {best_level['workers']} workers; {artifacts['G']['saturation_stop_reason']}.",
        "9_memory_footprint": f"Run schema occupied {sizes['schema_total_bytes']} bytes ({sizes['schema_total_bytes']/DATASET_SIZE:.1f} bytes/main memory including shared schema/fixture overhead).",
        "10_current_largest_bottleneck": "The current dominant measured bottleneck is exact vector scan/sort and associated service hydration, not an established disk or connection failure; resource snapshots and plans bound this conclusion.",
    }
    summary = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "COMPLETED",
               "finished_at": _utc(), "final_integrity": {key: int(value) for key, value in final_integrity.items()},
               "all_checkpoints_passed": True, "ann_indexes_present": False,
               "answers": answers,
               "evidence_level": "executed local 100K synthetic vector infrastructure benchmark",
               "what_this_proves": "observed behavior of the recorded PostgreSQL exact path at 100K",
               "what_this_does_not_prove": ["semantic retrieval quality", "production SLOs",
                   "true cold-cache performance", "ANN behavior", "1M scalability",
                   "PostgreSQL suitability or unsuitability in general"]}
    _write_json(ctx.run_dir / "final-summary.json", summary)
    report_path = Path(__file__).parents[2] / "docs" / "benchmark" / "postgres-exact-100k-report.md"
    ann_path = Path(__file__).parents[2] / "docs" / "benchmark" / "postgres-ann-experiment-plan.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(ctx, artifacts, sizes, summary), encoding="utf-8")
    ann_path.write_text(_ann_plan(), encoding="utf-8")
    ctx.manifest["status"] = "COMPLETED"
    ctx.manifest["finalized_at"] = _utc()
    ctx.manifest["final_artifacts"] = {
        name: _file_sha256(ctx.run_dir / name) for name in
        ("query-plans.json", "database-size.json", "resource-usage.json", "final-summary.json")}
    ctx.manifest["report"] = str(report_path.relative_to(Path(__file__).parents[2])).replace("\\", "/")
    ctx.manifest["ann_plan"] = str(ann_path.relative_to(Path(__file__).parents[2])).replace("\\", "/")
    _save_manifest(ctx)
    return summary


def close_partial(ctx: RunContext) -> dict[str, Any]:
    """Close a stopped experiment without retrying or fabricating missing work."""
    for key in "ABCDEF":
        if ctx.manifest["checkpoints"][key]["status"] != "PASS":
            raise RuntimeError(f"partial close requires completed checkpoint {key}")
    g_path = ctx.run_dir / CHECKPOINTS["G"]
    g = _read_json(g_path)
    error_count = sum(int(level.get("error_count", 0)) for level in g.get("levels", []))
    if not error_count:
        raise RuntimeError("partial close is only valid for the observed concurrency failure")
    attempt_path = ctx.run_dir / "concurrency-attempt-1.json"
    if ctx.manifest["checkpoints"]["G"]["status"] == "PASS":
        if attempt_path.exists():
            raise RuntimeError("preserved concurrency attempt already exists")
        g_path.replace(attempt_path)
        failed_g = dict(g)
        failed_g.update({
            "status": "FAILED", "finished_at": _utc(),
            "error": f"concurrency produced {error_count} sustained DiskFull errors in dynamic shared memory",
            "failure_mode": "PostgreSQL dynamic shared memory /dev/shm exhausted at 8 workers",
            "last_successful_checkpoint": "F",
            "raw_artifact_paths": ["concurrency-attempt-1.json"],
            "automatic_retry_performed": False,
        })
        digest = _write_json(g_path, failed_g)
        ctx.manifest["checkpoints"]["G"].update({
            "status": "FAILED", "finished_at": failed_g["finished_at"],
            "sha256": digest, "error": failed_g["error"],
            "preserved_attempt": "concurrency-attempt-1.json",
        })
        g = failed_g
    elif ctx.manifest["checkpoints"]["G"]["status"] != "FAILED":
        raise RuntimeError("checkpoint G is neither the observed PASS-with-errors nor FAILED")

    h = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "checkpoint": "H",
         "status": "NOT_COMPLETED", "timestamp": _utc(),
         "error": "not run because Checkpoint G had sustained dynamic shared memory errors",
         "last_successful_checkpoint": "F", "operation_count": 0,
         "workload_definition": {"planned": "70% retrieval / 20% ingest / 10% update-delete"}}
    h_digest = _write_json(ctx.run_dir / CHECKPOINTS["H"], h)
    ctx.manifest["checkpoints"]["H"].update({"status": "NOT_COMPLETED", "sha256": h_digest,
                                             "error": h["error"]})
    ctx.manifest["last_successful_checkpoint"] = "F"
    ctx.manifest["current_checkpoint"] = None

    artifacts = {key: _read_json(ctx.run_dir / CHECKPOINTS[key]) for key in "ABCDEFGH"}
    _ensure_main_count(ctx)
    with _connect(ctx) as connection:
        sizes = _db_sizes(connection, ctx.schema)
        final_integrity = connection.execute(
            "SELECT count(*) AS memories,count(v.*) AS vectors,min(vector_dims(v.embedding)) AS min_dimensions,"
            "max(vector_dims(v.embedding)) AS max_dimensions FROM memories m "
            "LEFT JOIN memory_vectors v ON v.memory_id=m.id WHERE m.namespace=%s",
            (MAIN_NAMESPACE,)).fetchone()
        indexes = list(connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname",
            (ctx.schema,)).fetchall())
    if any("hnsw" in row["indexdef"].lower() or "ivfflat" in row["indexdef"].lower()
           for row in indexes):
        raise RuntimeError("ANN index appeared before exact experiment closure")
    plans = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "COMPLETED",
             "exact": artifacts["B"]["query_plan"],
             "filtered": {key: value["query_plan"] for key, value in artifacts["C"]["treatments"].items()},
             "temporal": {key: value["query_plan"] for key, value in artifacts["D"]["treatments"].items()}}
    _write_json(ctx.run_dir / "query-plans.json", plans)
    size_artifact = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "COMPLETED",
                     "captured_at": _utc(), **sizes,
                     "bytes_per_main_memory": sizes["schema_total_bytes"] / DATASET_SIZE,
                     "dimension_basis": 96,
                     "calculated_estimates": {str(dim): {"label": "CALCULATED ESTIMATE",
                         "vector_payload_scale_only_bytes_per_memory": 4 * dim}
                         for dim in (768, 1024, 1536)},
                     "limitations": ["includes small fixture namespaces in the run schema",
                                     "dimension estimates are not benchmark results"]}
    _write_json(ctx.run_dir / "database-size.json", size_artifact)
    _write_json(ctx.run_dir / "resource-usage.json", {
        "schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "PARTIAL",
        "captured_at": _utc(),
        "checkpoints": {key: value.get("resources", {"available": False})
                        for key, value in artifacts.items()},
        "observed_failure": g["failure_mode"],
        "naming": ["host_memory", "docker_container", "benchmark_client"],
        "limitations": ["point snapshots", "PostgreSQL measured through Docker container"]})

    exact = artifacts["B"]["warm_query_latency_ms"]
    exact_ratio = float(exact["p50"]) / 167.1783
    temporal_current = artifacts["D"]["treatments"]["current_fact"]["latency_ms"]
    plan_nodes = sorted(set(_plan_node_types(plans)))
    answers = {
        "1_10K_to_100K_exact_latency": f"Observed 100K p50 {exact['p50']:.2f} ms versus 10K p50 167.18 ms ({exact_ratio:.2f}×); this is not directly comparable.",
        "2_is_scaling_linear": "No linear conclusion is supported: the observed ratio was not near 10× and the load/projection protocols and runtime state differ.",
        "3_dominant_time_component": "Exact vector scan/sort plus service candidate hydration dominates the service path; representative plans are preserved.",
        "4_metadata_filter_workload_reduction": "The 10% treatment reduced eligible candidates from 100,000 to 10,000 (90%); 1% and 50% treatments behaved consistently.",
        "5_temporal_extra_cost": f"Current-fact temporal SQL p50 was {temporal_current['p50']:.2f} ms versus exact service p50 {exact['p50']:.2f} ms; paths differ, so this is not a pure overhead ratio.",
        "6_planner_behavior": f"Observed plan node types: {', '.join(plan_nodes)}; no ANN index existed.",
        "7_bulk_vs_incremental": f"COPY achieved {artifacts['A']['bulk']['rows_per_second']:.2f} rows/s while single-request service ingest achieved {artifacts['E']['single_request']['memories_per_second']:.2f} memories/s; protocols are intentionally not equated.",
        "8_concurrency_first_saturation": "Dynamic shared memory (/dev/shm), not the database volume, saturated first at 8 workers; 44/64 operations failed and the experiment stopped.",
        "9_memory_footprint": f"Final run schema size was {sizes['schema_total_bytes']} bytes ({sizes['schema_total_bytes']/DATASET_SIZE:.1f} bytes per main memory including schema/fixture overhead) for 96-d vectors.",
        "10_current_largest_bottleneck": "At single-query load the dominant path is exact vector scan/sort and service hydration; under concurrency the hard bottleneck was the container's 64 MiB dynamic shared-memory mount.",
    }
    summary = {"schema_version": SCHEMA_VERSION, "run_id": ctx.run_id, "status": "PARTIAL",
               "finished_at": _utc(), "last_successful_checkpoint": "F",
               "failed_checkpoint": "G", "not_completed_checkpoint": "H",
               "failure_mode": g["failure_mode"],
               "final_integrity": {key: int(value) for key, value in final_integrity.items()},
               "ann_indexes_present": False, "answers": answers,
               "evidence_level": "executed A-F; failed G; H not executed by stop condition",
               "what_this_proves": "observed exact, filter, temporal, ingest, delete and concurrency saturation behavior",
               "what_this_does_not_prove": ["semantic quality", "successful mixed workload",
                   "true cold cache", "ANN behavior", "1M scalability", "general PostgreSQL scalability"]}
    _write_json(ctx.run_dir / "final-summary.json", summary)

    c = artifacts["C"]["treatments"]
    d = artifacts["D"]["treatments"]
    report = f"""# PostgreSQL exact 100K benchmark report

Run ID: `{ctx.run_id}`  
Status: **PARTIAL — stopped at Checkpoint G**

## Scope

This is a 100,000-row PostgreSQL + pgvector synthetic-vector infrastructure benchmark. It does not measure semantic embedding quality or real-world recall.

## Environment

The recorded PostgreSQL, pgvector, Docker, host and database settings are in `environment.json`. At 8 workers PostgreSQL exhausted the container's 64 MiB dynamic shared-memory mount; the database volume remained healthy with ample free space.

## Dataset

The main namespace contains exactly 100,000 memories and 100,000 96-dimensional deterministic synthetic vectors. Metadata provides 1%, 10% and 50% cohorts; temporal windows are deterministic. See `dataset.json`.

## Methodology

Every stage ran as a separate command and published a JSON artifact. Fresh-connection first-query is a proxy, not true cold cache. No HNSW or IVFFlat index was created. The experiment stopped on sustained concurrency errors and did not run the mixed workload.

## Bulk Load

Generation took {artifacts['A']['bulk']['dataset_generation_seconds']:.2f} s; database insert took {artifacts['A']['bulk']['bulk_insert_seconds']:.2f} s ({artifacts['A']['bulk']['rows_per_second']:.2f} rows/s). This COPY rate is not production ingest throughput.

## Incremental Ingest

Single service writes achieved {artifacts['E']['single_request']['memories_per_second']:.2f} memories/s with p50 {artifacts['E']['single_request']['latency_ms']['p50']:.2f} ms and p95 {artifacts['E']['single_request']['latency_ms']['p95']:.2f} ms. Four writers achieved {artifacts['E']['concurrent_writers']['memories_per_second']:.2f} memories/s with zero failures; idempotency passed.

## Exact Retrieval

100 warm service queries measured p50 {exact['p50']:.2f} ms, p95 {exact['p95']:.2f} ms and p99 {exact['p99']:.2f} ms. The exact-string fixture Top-1 rate was 100%. The fresh-connection first-query proxy was {artifacts['B']['first_query_proxy']['latency_ms']:.2f} ms.

## Filtered Retrieval

- 1% / 1,000 candidates: p50 {c['high_selectivity_1_percent']['latency_ms']['p50']:.2f} ms.
- 10% / 10,000 candidates: p50 {c['medium_selectivity_10_percent']['latency_ms']['p50']:.2f} ms.
- 50% / 50,000 candidates: p50 {c['low_selectivity_50_percent']['latency_ms']['p50']:.2f} ms.

These are direct SQL infrastructure measurements because the service predicate channel is not a hard metadata filter.

## Temporal Retrieval

- Current (80,000 eligible): p50 {d['current_fact']['latency_ms']['p50']:.2f} ms.
- Historical (5,555 eligible): p50 {d['historical_fact']['latency_ms']['p50']:.2f} ms.
- Window overlap (24,970 eligible): p50 {d['window_overlap']['latency_ms']['p50']:.2f} ms.

All direct-SQL fixture results and service current/historical checks were correct. SQL performance and service correctness are separate evidence.

## Delete

On an isolated fixture, soft delete took {artifacts['F']['soft_delete_latency_ms']:.2f} ms and hard purge {artifacts['F']['hard_purge_latency_ms']:.2f} ms. Row/vector/projection cleanup, tombstones and rollback passed; the main 100K namespace was untouched.

## Concurrency

1 worker: {g['levels'][0]['throughput_queries_per_second']:.2f} q/s. 4 workers: {g['levels'][1]['throughput_queries_per_second']:.2f} q/s. At 8 workers, {error_count} of {g['levels'][2]['operation_count']} operations failed because PostgreSQL could not resize dynamic shared-memory segments in `/dev/shm`. `SKIP LOCKED` disjoint-claim correctness passed. The checkpoint status is FAILED.

## Mixed Workload

**NOT COMPLETED.** The planned 70/20/10 read/ingest/delete workload was not run after Checkpoint G crossed the sustained-error stop condition.

## Resource Usage

Host memory, benchmark-client RSS and Docker container snapshots remain separately named in `resource-usage.json`. The database volume had ample space; `/dev/shm` was the saturated resource during concurrency.

## Database Size

The run schema occupied {sizes['schema_total_bytes']} bytes: tables {sizes['schema_table_bytes']}, indexes {sizes['schema_index_bytes']}, TOAST {sizes['schema_toast_bytes']}. This is a 96-d result; 768/1024/1536 values in `database-size.json` are labelled CALCULATED ESTIMATE.

## Query Plans

Representative `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` evidence for exact, three filter selectivities and three temporal treatments is in `query-plans.json`.

## Comparison With 10K

The old 10K service baseline was 38.62 memories/s and retrieval p50/p95/p99 167.18/176.78/179.36 ms. The 100K retrieval result is **not directly comparable** because the 100K main corpus was bulk-loaded without the full event/provenance projections, runtime state differed, and the sample count changed from 20 to 100. No linear-scaling claim is made.

## Observed Bottlenecks

Exact scan/sort and service hydration dominate individual retrieval. Under concurrency, the first hard saturation was Docker's 64 MiB `/dev/shm` allocation for PostgreSQL parallel work.

## Limitations

- Synthetic 96-d vectors; no semantic-quality claim.
- Cache state was not controlled at PostgreSQL shared buffers and OS page cache.
- Resource measurements are point snapshots.
- Filter/temporal SQL timing is separate from current service behavior.
- Mixed workload was not run because the experiment stopped at G.

## Evidence Level

A-F are completed local evidence; G is a retained failure; H is explicitly NOT_COMPLETED. This is a partial benchmark, not a successful end-to-end 100K workload certification.

## What This Benchmark Proves

It proves the recorded local behavior for 100K bulk storage, exact retrieval, SQL filtering, temporal paths, real service ingest, deletion and the concurrency failure boundary.

## What This Benchmark Does NOT Prove

It does not prove semantic retrieval quality, a successful mixed workload, production SLOs, true cold-cache behavior, ANN behavior, 1M scaling, or that PostgreSQL generally can or cannot scale.

## Required Questions

""" + "\n".join(f"- **{key}**: {value}" for key, value in answers.items()) + "\n"
    report_path = Path(__file__).parents[2] / "docs" / "benchmark" / "postgres-exact-100k-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    ctx.manifest["status"] = "PARTIAL"
    ctx.manifest["terminal_reason"] = g["failure_mode"]
    ctx.manifest["closed_at"] = _utc()
    ctx.manifest["ann_plan_status"] = "NOT_CREATED: exact experiment did not complete through H"
    ctx.manifest["final_artifacts"] = {
        name: _file_sha256(ctx.run_dir / name) for name in
        ("query-plans.json", "database-size.json", "resource-usage.json", "final-summary.json")}
    ctx.manifest["report"] = str(report_path.relative_to(Path(__file__).parents[2])).replace("\\", "/")
    _save_manifest(ctx)
    return summary


def _default_run_id() -> str:
    return datetime.now().strftime("postgres-100k-%Y%m%d-%H%M%S")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Checkpointed PostgreSQL 100K exact-path benchmark; never runs all checkpoints implicitly")
    parser.add_argument("command", choices=("init", "status", "smoke", "checkpoint-a", "checkpoint-b",
                                            "checkpoint-c", "checkpoint-d", "checkpoint-e", "checkpoint-f",
                                            "checkpoint-g", "checkpoint-h", "finalize", "close-partial"))
    parser.add_argument("--run-id")
    parser.add_argument("--reports-root", type=Path, default=Path("eval/reports/postgres-100k"))
    parser.add_argument("--container", default="dive-memory-phase3-pg-20260920")
    args = parser.parse_args()
    dsn = os.environ.get("DIVE_TEST_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("set DIVE_TEST_POSTGRES_DSN; it is never written to artifacts")
    run_id = args.run_id or (_default_run_id() if args.command == "init" else None)
    if not run_id:
        raise SystemExit("--run-id is required after init")
    if args.command == "init":
        ctx = init_run(run_id, args.reports_root, dsn, args.container)
        print(json.dumps({"run_id": ctx.run_id, "run_dir": str(ctx.run_dir),
                          "schema": ctx.schema, "status": ctx.manifest["status"]}, ensure_ascii=False))
        return
    ctx = _load_context(run_id, args.reports_root, dsn, args.container)
    if args.command == "status":
        print(json.dumps(ctx.manifest, ensure_ascii=False, indent=2))
        return
    if args.command == "smoke":
        result = smoke_test(ctx)
    elif args.command == "finalize":
        result = finalize(ctx)
    elif args.command == "close-partial":
        result = close_partial(ctx)
    else:
        key = args.command[-1].upper()
        functions: dict[str, Callable[[RunContext], dict[str, Any]]] = {
            "A": checkpoint_a, "B": checkpoint_b, "C": checkpoint_c, "D": checkpoint_d,
            "E": checkpoint_e, "F": checkpoint_f, "G": checkpoint_g, "H": checkpoint_h,
        }
        result = _checkpoint(ctx, key, functions[key])
    print(json.dumps({"run_id": run_id, "command": args.command,
                      "status": result.get("status", "COMPLETED"),
                      "artifact_dir": str(ctx.run_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
