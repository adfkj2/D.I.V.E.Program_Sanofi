"""Paired PostgreSQL parallelism benchmark with a shared immutable schedule.

Each CLI invocation executes at most one worker-level/trial pair.  This module
is deliberately separate from the concurrency recovery runner and its evidence.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import threading
from time import perf_counter
from typing import Any, Iterable

from .benchmark import _percentile
from .ids import stable_vector


SCHEMA_VERSION = "dive-postgres-paired-parallelism-v1"
DEFAULT_REPORTS_ROOT = Path("eval/reports/postgres-parallelism")
DEFAULT_CONTAINER = "dive-memory-postgres-100k-concurrency"
DEFAULT_SCHEMA = "dive_postgres_100k_f3d888f630bd7ba8"
DEFAULT_NAMESPACE = "postgres-100k-main"
DEFAULT_SEED = 20260960
DATASET_SEED = 20260920
WORKER_LEVELS = (1, 4, 8)
TRIALS = (1, 2, 3, 4)
OPERATIONS_PER_WORKER = 32
WARMUPS_PER_WORKER = 2
ARMS = ("parallel", "no-parallel")
ARM_SETTINGS = {"parallel": 2, "no-parallel": 0}
COUNTERBALANCED_ORDER = {
    1: ("parallel", "no-parallel"),
    2: ("no-parallel", "parallel"),
    3: ("parallel", "no-parallel"),
    4: ("no-parallel", "parallel"),
}
PREREGISTRATION = Path("docs/benchmark/postgres-paired-parallelism-experiment-plan.md")
PROTOCOL_ISSUE = Path("docs/benchmark/postgres-parallelism-protocol-issue.md")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{hashlib.sha256(payload.encode()).hexdigest()[:12]}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(path)
    json.loads(path.read_text(encoding="utf-8"))
    return file_sha256(path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def schedule_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "query_schedule_sha256"}


def schedule_sha256(document: dict[str, Any]) -> str:
    return sha256_value(schedule_payload(document))


def verify_schedule(document: dict[str, Any]) -> str:
    recorded = document.get("query_schedule_sha256")
    actual = schedule_sha256(document)
    if not recorded or recorded != actual:
        raise RuntimeError(f"query schedule SHA mismatch: recorded={recorded} actual={actual}")
    return actual


def generate_query_schedule(
    run_id: str,
    *,
    dataset_size: int = 100_000,
    worker_levels: Iterable[int] = WORKER_LEVELS,
    trials: Iterable[int] = TRIALS,
    operations_per_worker: int = OPERATIONS_PER_WORKER,
    warmups_per_worker: int = WARMUPS_PER_WORKER,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Generate targets once.  Arms never appear in the schedule or RNG path."""
    rng = random.Random(seed)
    entries: list[dict[str, Any]] = []
    for workers in worker_levels:
        for trial in trials:
            for worker_id in range(workers):
                for phase, count in (("warmup", warmups_per_worker), ("timed", operations_per_worker)):
                    for operation_index in range(count):
                        target = rng.randrange(dataset_size)
                        entries.append({
                            "worker_level": workers,
                            "trial_id": trial,
                            "worker_id": worker_id,
                            "phase": phase,
                            "operation_index": operation_index,
                            "target_memory_id": target,
                            "parameters": {
                                "namespace": DEFAULT_NAMESPACE,
                                "top_k": 5,
                                "dataset_seed": DATASET_SEED,
                            },
                            "schedule_seed": seed,
                        })
    document = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "schedule_seed": seed,
        "dataset_size": dataset_size,
        "worker_levels": list(worker_levels),
        "trials": list(trials),
        "operations_per_worker": operations_per_worker,
        "warmups_per_worker": warmups_per_worker,
        "entries": entries,
    }
    document["query_schedule_sha256"] = schedule_sha256(document)
    return document


def generate_execution_order(run_id: str, trials: Iterable[int] = TRIALS) -> dict[str, Any]:
    rows = [{"trial_id": trial, "order": list(COUNTERBALANCED_ORDER[trial])} for trial in trials]
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "counterbalanced": True, "trials": rows}


def entries_for_arm(schedule: dict[str, Any], workers: int, trial: int, arm: str) -> list[dict[str, Any]]:
    """Return the shared entries; ``arm`` is validated but cannot alter them."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    verify_schedule(schedule)
    return [dict(row) for row in schedule["entries"]
            if row["worker_level"] == workers and row["trial_id"] == trial]


def target_lists(schedule: dict[str, Any], workers: int, trial: int, arm: str) -> dict[str, list[int]]:
    rows = entries_for_arm(schedule, workers, trial, arm)
    return {
        phase: [row["target_memory_id"] for row in rows if row["phase"] == phase]
        for phase in ("warmup", "timed")
    }


def apply_session_setting(connection: Any, arm: str) -> int:
    setting = ARM_SETTINGS[arm]
    connection.execute("SELECT set_config('max_parallel_workers_per_gather', %s, false)", (str(setting),))
    return setting


def _walk_plan(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "Node Type" in value:
            yield value
        for child in value.values():
            yield from _walk_plan(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_plan(child)


def validate_query_plan(plan: Any, arm: str, *, require_parallel_execution: bool = True) -> dict[str, Any]:
    nodes = list(_walk_plan(plan))
    node_types = [str(node.get("Node Type", "")) for node in nodes]
    gathers = [name for name in node_types if name in {"Gather", "Gather Merge"}]
    parallel_nodes = [name for name in node_types if "Parallel" in name]
    parallel_aware = any(node.get("Parallel Aware") is True for node in nodes)
    workers_planned = sum(int(node.get("Workers Planned", 0) or 0) for node in nodes)
    workers_launched = sum(int(node.get("Workers Launched", 0) or 0) for node in nodes)
    if arm == "parallel":
        structural = bool(gathers or parallel_nodes or parallel_aware or workers_planned)
        passed = (structural and workers_launched > 0) if require_parallel_execution else True
    elif arm == "no-parallel":
        passed = not (gathers or parallel_nodes or parallel_aware or workers_planned or workers_launched)
    else:
        raise ValueError(f"unknown arm: {arm}")
    return {
        "status": "PASS" if passed else "FAIL",
        "node_types": node_types,
        "gather_nodes": gathers,
        "parallel_nodes": parallel_nodes,
        "parallel_aware": parallel_aware,
        "workers_planned": workers_planned,
        "workers_launched": workers_launched,
        "require_parallel_execution": require_parallel_execution,
    }


def _summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0,
                "min": 0.0, "max": 0.0, "p95_minus_p50": 0.0, "p99_minus_p50": 0.0}
    p50, p95, p99 = statistics.median(values), _percentile(values, .95), _percentile(values, .99)
    return {"count": len(values), "p50": p50, "p95": p95, "p99": p99,
            "mean": statistics.mean(values), "min": min(values), "max": max(values),
            "p95_minus_p50": p95 - p50, "p99_minus_p50": p99 - p50}


def _run_command(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _code_fingerprint(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path(__file__).parents[2]
    files: list[Path] = []
    for directory in (root / "src", root / "tests", root / "migrations"):
        if directory.exists():
            files.extend(path for path in directory.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    for path in (root / "pyproject.toml", root / PREREGISTRATION, root / PROTOCOL_ISSUE):
        if path.exists():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big") + relative)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    head = _run_command(["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"])
    return {"git_head": head.stdout.strip() if head and head.returncode == 0 else "NOT_AVAILABLE",
            "experiment_tree_sha256": digest.hexdigest(), "experiment_tree_file_count": len(set(files)),
            "scope": ["src", "tests", "migrations", "pyproject.toml", str(PREREGISTRATION), str(PROTOCOL_ISSUE)]}


def _schema_dsn(dsn: str, schema: str) -> str:
    from psycopg.conninfo import make_conninfo
    return make_conninfo(dsn, options=f"-c search_path={schema},public")


def connect(dsn: str, schema: str, arm: str | None = None):
    import psycopg
    from pgvector.psycopg import register_vector
    from psycopg.rows import dict_row
    connection = psycopg.connect(_schema_dsn(dsn, schema), autocommit=True, row_factory=dict_row)
    register_vector(connection)
    if arm is not None:
        apply_session_setting(connection, arm)
    return connection


def _query(connection: Any, target: int, namespace: str = DEFAULT_NAMESPACE, top_k: int = 5) -> list[dict[str, Any]]:
    from pgvector import Vector
    text = f"benchmark memory {target:06d} user {target % 100:03d} seed {DATASET_SEED}"
    vector = Vector(stable_vector(text, dimensions=96))
    return list(connection.execute(
        "SELECT m.id,m.content,1.0-(v.embedding <=> %s) AS cosine_similarity "
        "FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
        "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') "
        "ORDER BY v.embedding <=> %s,m.id LIMIT %s",
        (vector, namespace, vector, top_k),
    ).fetchall())


def _explain_query(connection: Any, target: int, namespace: str = DEFAULT_NAMESPACE) -> Any:
    from pgvector import Vector
    text = f"benchmark memory {target:06d} user {target % 100:03d} seed {DATASET_SEED}"
    vector = Vector(stable_vector(text, dimensions=96))
    row = connection.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
        "SELECT m.id,m.content,1.0-(v.embedding <=> %s) AS cosine_similarity "
        "FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
        "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') "
        "ORDER BY v.embedding <=> %s,m.id LIMIT 5",
        (vector, namespace, vector),
    ).fetchone()
    return next(iter(row.values()))


def _counts(connection: Any, namespace: str) -> dict[str, int]:
    row = connection.execute(
        "SELECT count(*) AS memories,count(v.*) AS vectors FROM memories m "
        "LEFT JOIN memory_vectors v ON v.memory_id=m.id WHERE m.namespace=%s", (namespace,)).fetchone()
    return {"memories": int(row["memories"]), "vectors": int(row["vectors"])}


def _resource_snapshot(container: str, connection: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"captured_at": utc_now(), "sampling": "point-in-time; not peak sampling"}
    stats = _run_command(["docker", "stats", container, "--no-stream", "--format", "{{json .}}"])
    if stats and stats.returncode == 0:
        try:
            row = json.loads(stats.stdout.strip().splitlines()[-1])
            result["container"] = {"cpu_percent": row.get("CPUPerc"), "memory_usage": row.get("MemUsage"),
                                   "memory_percent": row.get("MemPerc")}
        except Exception as exc:
            result["container"] = {"status": "NOT_AVAILABLE", "reason": str(exc)}
    else:
        result["container"] = {"status": "NOT_AVAILABLE"}
    shm = _run_command(["docker", "exec", container, "sh", "-c", "df -B1 /dev/shm | tail -1"])
    if shm and shm.returncode == 0 and len(shm.stdout.split()) >= 4:
        fields = shm.stdout.split()
        result["shared_memory"] = {"total_bytes": int(fields[1]), "used_bytes": int(fields[2]),
                                   "available_bytes": int(fields[3])}
    else:
        result["shared_memory"] = {"status": "NOT_AVAILABLE"}
    activity = connection.execute(
        "SELECT count(*) AS connections,count(*) FILTER (WHERE state='active') AS active_queries,"
        "count(*) FILTER (WHERE wait_event IS NOT NULL) AS waiting FROM pg_stat_activity"
    ).fetchone()
    locks = connection.execute("SELECT count(*) FILTER (WHERE NOT granted) AS ungranted FROM pg_locks").fetchone()
    result["postgres"] = {"connections": int(activity["connections"]),
                          "active_queries": int(activity["active_queries"]), "waiting": int(activity["waiting"]),
                          "ungranted_locks": int(locks["ungranted"])}
    result["disk_free_bytes"] = shutil.disk_usage(Path.cwd()).free
    return result


def create_run(run_dir: Path, run_id: str, *, dataset_size: int = 100_000,
               worker_levels: Iterable[int] = WORKER_LEVELS, trials: Iterable[int] = TRIALS,
               operations_per_worker: int = OPERATIONS_PER_WORKER,
               warmups_per_worker: int = WARMUPS_PER_WORKER, seed: int = DEFAULT_SEED,
               schema: str = DEFAULT_SCHEMA, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    if (run_dir / "manifest.json").exists():
        return read_json(run_dir / "manifest.json")
    worker_levels = tuple(worker_levels)
    trials = tuple(trials)
    schedule = generate_query_schedule(run_id, dataset_size=dataset_size, worker_levels=worker_levels,
                                       trials=trials, operations_per_worker=operations_per_worker,
                                       warmups_per_worker=warmups_per_worker, seed=seed)
    order = generate_execution_order(run_id, trials)
    atomic_write_json(run_dir / "query-schedule.json", schedule)
    atomic_write_json(run_dir / "execution-order.json", order)
    checkpoints = {}
    for workers in worker_levels:
        checkpoints[str(workers)] = {str(trial): {arm: {"status": "PENDING", "attempt_count": 0}
                                                    for arm in ARMS} | {"pair_status": "PENDING"}
                                      for trial in trials}
    manifest = {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "PENDING", "created_at": utc_now(),
        "config": {"dataset_size": dataset_size, "worker_levels": list(worker_levels), "trials": list(trials),
                   "operations_per_worker": operations_per_worker, "warmups_per_worker": warmups_per_worker,
                   "schedule_seed": seed, "schema": schema, "namespace": namespace,
                   "parallel_setting": 2, "no_parallel_setting": 0, "retries": 0},
        "query_schedule_sha256": schedule["query_schedule_sha256"],
        "artifacts": {"query_schedule": {"path": "query-schedule.json", "sha256": file_sha256(run_dir / "query-schedule.json")},
                      "execution_order": {"path": "execution-order.json", "sha256": file_sha256(run_dir / "execution-order.json")}},
        "checkpoints": checkpoints,
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    return manifest


def ensure_environment(run_dir: Path, dsn: str, container: str, *, expected_shm: int | None = 512 * 1024 * 1024) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    path = run_dir / "environment.json"
    current_code = _code_fingerprint()
    if path.exists():
        saved = read_json(path)
        if saved.get("code_fingerprint", {}).get("experiment_tree_sha256") != current_code["experiment_tree_sha256"]:
            raise RuntimeError("code fingerprint changed after experiment initialization")
        if saved.get("status") != "PASS":
            raise RuntimeError("saved environment did not pass")
        return saved
    cfg = manifest["config"]
    with connect(dsn, cfg["schema"], "no-parallel") as connection:
        counts = _counts(connection, cfg["namespace"])
        versions = {"postgres": str(next(iter(connection.execute("SHOW server_version").fetchone().values()))),
                    "pgvector": str(connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()["extversion"])}
        dimensions = [int(row["dimensions"]) for row in connection.execute(
            "SELECT DISTINCT vector_dims(v.embedding) AS dimensions FROM memory_vectors v "
            "JOIN memories m ON m.id=v.memory_id WHERE m.namespace=%s", (cfg["namespace"],)).fetchall()]
        dataset_row = connection.execute(
            "SELECT count(*) FILTER (WHERE m.structured_content->>'cohort_1'='true') AS cohort_1,"
            "count(*) FILTER (WHERE m.structured_content->>'cohort_10'='true') AS cohort_10,"
            "count(*) FILTER (WHERE m.structured_content->>'cohort_50'='true') AS cohort_50,"
            "count(*) FILTER (WHERE m.structured_content->>'seed' IS DISTINCT FROM %s) AS seed_mismatch "
            "FROM memories m WHERE m.namespace=%s", (str(DATASET_SEED), cfg["namespace"])).fetchone()
        cohorts = {"cohort_1": int(dataset_row["cohort_1"]), "cohort_10": int(dataset_row["cohort_10"]),
                   "cohort_50": int(dataset_row["cohort_50"]), "seed_mismatch": int(dataset_row["seed_mismatch"])}
        expected_cohorts = {"cohort_1": max(1, cfg["dataset_size"] // 100),
                            "cohort_10": max(1, cfg["dataset_size"] // 10),
                            "cohort_50": max(1, cfg["dataset_size"] // 2), "seed_mismatch": 0}
        settings = {name: str(next(iter(connection.execute(f"SHOW {name}").fetchone().values()))) for name in
                    ("shared_buffers", "work_mem", "max_parallel_workers", "max_parallel_workers_per_gather", "max_worker_processes")}
        resources = _resource_snapshot(container, connection)
    inspect = _run_command(["docker", "inspect", container, "--format", "{{json .State}}|{{.HostConfig.ShmSize}}"])
    inspect_text = inspect.stdout.strip() if inspect and inspect.returncode == 0 else ""
    try:
        state_text, inspect_shm_text = inspect_text.rsplit("|", 1)
        container_state = json.loads(state_text)
        inspect_shm = int(inspect_shm_text)
    except Exception:
        container_state, inspect_shm = {}, None
    shm_total = resources.get("shared_memory", {}).get("total_bytes")
    valid = (counts == {"memories": cfg["dataset_size"], "vectors": cfg["dataset_size"]}
             and dimensions == [96] and cohorts == expected_cohorts
             and versions["postgres"].startswith("16.15") and versions["pgvector"] == "0.8.6"
             and settings["shared_buffers"] == "128MB" and settings["work_mem"] == "4MB"
             and settings["max_parallel_workers"] == "8" and settings["max_worker_processes"] == "8"
             and settings["max_parallel_workers_per_gather"] == "0"
             and container_state.get("Running") is True and container_state.get("OOMKilled") is False
             and int(container_state.get("RestartCount", 0) or 0) == 0
             and (expected_shm is None or (shm_total == expected_shm and inspect_shm == expected_shm)))
    environment = {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"],
                   "status": "PASS" if valid else "FAIL", "captured_at": utc_now(), "dataset": counts,
                   "embedding_dimensions": dimensions, "metadata_cohorts": cohorts,
                   "expected_metadata_cohorts": expected_cohorts, "versions": versions,
                   "postgres_settings_no_parallel_session": settings,
                   "container": {"state": container_state, "shm_size_bytes": inspect_shm}, "resources": resources,
                   "expected_shm_bytes": expected_shm, "code_fingerprint": current_code, "dsn_recorded": False}
    atomic_write_json(path, environment)
    if not valid:
        raise RuntimeError(f"environment preflight failed: {environment}")
    return environment


def arm_artifact_path(run_dir: Path, workers: int, trial: int, arm: str) -> Path:
    return run_dir / f"workers-{workers}" / f"trial-{trial}" / f"{arm}.json"


def _classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "dynamic shared memory" in text or "dsm" in text: return "DSM_ERROR"
    if "shared memory" in text: return "SHARED_MEMORY_ERROR"
    if "deadlock" in text: return "DEADLOCK"
    if "timeout" in text: return "TIMEOUT"
    if "connection" in text or "server closed" in text: return "CONNECTION_ERROR"
    return "DATABASE_ERROR"


def execute_trial_arm(run_dir: Path, dsn: str, container: str, workers: int, trial: int, arm: str,
                      *, require_parallel_execution: bool = True) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    cfg = manifest["config"]
    record = manifest["checkpoints"][str(workers)][str(trial)][arm]
    path = arm_artifact_path(run_dir, workers, trial, arm)
    schedule = read_json(run_dir / "query-schedule.json")
    digest = verify_schedule(schedule)
    if digest != manifest["query_schedule_sha256"]:
        raise RuntimeError("manifest/query schedule SHA mismatch blocks comparison")
    if record["status"] == "PASS":
        artifact = read_json(path)
        if artifact.get("query_schedule_sha256") != digest:
            raise RuntimeError("completed artifact schedule SHA mismatch")
        return artifact
    if record["status"] in {"FAILED", "INVALID"}:
        raise RuntimeError(f"arm is terminal and will not be retried: {record['status']}")
    record.update({"status": "RUNNING", "attempt_count": int(record["attempt_count"]) + 1, "started_at": utc_now()})
    atomic_write_json(run_dir / "manifest.json", manifest)
    rows = entries_for_arm(schedule, workers, trial, arm)
    warmups = [row for row in rows if row["phase"] == "warmup"]
    timed = [row for row in rows if row["phase"] == "timed"]
    try:
        with connect(dsn, cfg["schema"], arm) as preflight:
            effective = int(next(iter(preflight.execute("SHOW max_parallel_workers_per_gather").fetchone().values())))
            if effective != ARM_SETTINGS[arm]:
                raise RuntimeError(f"session setting mismatch: expected {ARM_SETTINGS[arm]}, got {effective}")
            plan = _explain_query(preflight, warmups[0]["target_memory_id"], cfg["namespace"])
            plan_validation = validate_query_plan(plan, arm, require_parallel_execution=require_parallel_execution)
            if plan_validation["status"] != "PASS":
                raise RuntimeError(f"query plan validation failed: {plan_validation}")
            for row in warmups[1:]:
                _query(preflight, row["target_memory_id"], cfg["namespace"])
            counts_before = _counts(preflight, cfg["namespace"])
            resources_before = _resource_snapshot(container, preflight)
        barrier = threading.Barrier(workers)

        def run_worker(worker_id: int) -> list[dict[str, Any]]:
            assigned = [row for row in timed if row["worker_id"] == worker_id]
            results: list[dict[str, Any]] = []
            with connect(dsn, cfg["schema"], arm) as connection:
                observed = int(next(iter(connection.execute("SHOW max_parallel_workers_per_gather").fetchone().values())))
                if observed != ARM_SETTINGS[arm]:
                    raise RuntimeError("worker session setting mismatch")
                barrier.wait(timeout=30)
                for row in assigned:
                    started = perf_counter()
                    try:
                        found = _query(connection, row["target_memory_id"], cfg["namespace"])
                        actual = [item["id"] for item in found]
                        expected = f"mem_bench_{row['target_memory_id']:06d}"
                        results.append({"worker_id": worker_id, "operation_index": row["operation_index"],
                                        "target_memory_id": row["target_memory_id"],
                                        "latency_ms": (perf_counter() - started) * 1000, "success": True,
                                        "correct": bool(actual and actual[0] == expected), "expected_top1": expected,
                                        "actual_ids": actual, "error_class": None})
                    except Exception as exc:
                        results.append({"worker_id": worker_id, "operation_index": row["operation_index"],
                                        "target_memory_id": row["target_memory_id"],
                                        "latency_ms": (perf_counter() - started) * 1000, "success": False,
                                        "correct": False, "expected_top1": f"mem_bench_{row['target_memory_id']:06d}",
                                        "actual_ids": [], "error_class": _classify_error(exc),
                                        "error": f"{type(exc).__name__}: {exc}"})
            return results

        started_at = utc_now()
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            grouped = list(executor.map(run_worker, range(workers)))
        duration = perf_counter() - started
        operations = [row for group in grouped for row in group]
        with connect(dsn, cfg["schema"], arm) as after:
            counts_after = _counts(after, cfg["namespace"])
            resources_after = _resource_snapshot(container, after)
        failures = sum(not row["success"] for row in operations)
        correct = all(row["correct"] for row in operations) and counts_before == counts_after
        latencies = [row["latency_ms"] for row in operations if row["success"]]
        errors = [row["error_class"] for row in operations if row.get("error_class")]
        status = "PASS" if failures == 0 and correct else "FAILED"
        artifact = {
            "schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "status": status,
            "workers": workers, "trial": trial, "arm": arm, "session_max_parallel_workers_per_gather": effective,
            "query_schedule_sha256": digest, "started_at": started_at, "finished_at": utc_now(),
            "warmup": {"count": len(warmups), "target_memory_ids": [row["target_memory_id"] for row in warmups],
                       "excluded_from_metrics": True},
            "timed_target_memory_ids": [row["target_memory_id"] for row in timed],
            "plan": plan, "plan_validation": plan_validation,
            "metrics": {"operations": len(operations), "successes": len(operations) - failures,
                        "failures": failures, "failure_rate": failures / len(operations) if operations else 1.0,
                        "throughput_operations_per_second": (len(operations) - failures) / max(duration, 1e-9),
                        "latency_ms": _summary(latencies)},
            "correctness": {"status": "PASS" if correct else "FAIL", "read_consistent": all(row["correct"] for row in operations),
                            "counts_before": counts_before, "counts_after": counts_after,
                            "unexpected_row_delta": {key: counts_after[key] - counts_before[key] for key in counts_before}},
            "errors": {"classes": {name: errors.count(name) for name in sorted(set(errors))},
                       "timeouts": errors.count("TIMEOUT"), "deadlocks": errors.count("DEADLOCK"),
                       "dsm": errors.count("DSM_ERROR"), "shared_memory": errors.count("SHARED_MEMORY_ERROR"),
                       "connections": errors.count("CONNECTION_ERROR"), "retries": 0},
            "resources": {"before": resources_before, "after": resources_after}, "operations": operations,
        }
        artifact_digest = atomic_write_json(path, artifact)
        manifest = read_json(run_dir / "manifest.json")
        manifest["checkpoints"][str(workers)][str(trial)][arm].update(
            {"status": status, "finished_at": utc_now(), "artifact": str(path.relative_to(run_dir)).replace("\\", "/"),
             "sha256": artifact_digest})
        atomic_write_json(run_dir / "manifest.json", manifest)
        return artifact
    except Exception as exc:
        failed = {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "status": "INVALID",
                  "workers": workers, "trial": trial, "arm": arm, "query_schedule_sha256": digest,
                  "finished_at": utc_now(), "reason": f"{type(exc).__name__}: {exc}"}
        artifact_digest = atomic_write_json(path, failed)
        manifest = read_json(run_dir / "manifest.json")
        manifest["checkpoints"][str(workers)][str(trial)][arm].update(
            {"status": "INVALID", "finished_at": utc_now(), "sha256": artifact_digest})
        atomic_write_json(run_dir / "manifest.json", manifest)
        raise


def compare_pair(parallel: dict[str, Any], no_parallel: dict[str, Any]) -> dict[str, Any]:
    hashes = {parallel.get("query_schedule_sha256"), no_parallel.get("query_schedule_sha256")}
    valid = (len(hashes) == 1 and None not in hashes and parallel.get("status") == "PASS"
             and no_parallel.get("status") == "PASS"
             and parallel.get("correctness", {}).get("status") == "PASS"
             and no_parallel.get("correctness", {}).get("status") == "PASS"
             and parallel.get("timed_target_memory_ids") == no_parallel.get("timed_target_memory_ids"))
    if not valid:
        return {"status": "INVALID", "reason": "schedule, target, status, or correctness mismatch"}
    pm, nm = parallel["metrics"], no_parallel["metrics"]
    return {"status": "PASS", "query_schedule_sha256": parallel["query_schedule_sha256"],
            "delta_parallel_minus_no_parallel": {
                "throughput": pm["throughput_operations_per_second"] - nm["throughput_operations_per_second"],
                **{name: pm["latency_ms"][name] - nm["latency_ms"][name] for name in ("p50", "p95", "p99")}}}


def aggregate_trials(run_dir: Path) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    summary: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "workers": {}}
    for worker_text, trials in manifest["checkpoints"].items():
        valid_pairs = []
        for trial_text in trials:
            p_path = arm_artifact_path(run_dir, int(worker_text), int(trial_text), "parallel")
            np_path = arm_artifact_path(run_dir, int(worker_text), int(trial_text), "no-parallel")
            if p_path.exists() and np_path.exists():
                pair = compare_pair(read_json(p_path), read_json(np_path))
                if pair["status"] == "PASS": valid_pairs.append(pair)
        metrics: dict[str, Any] = {}
        for name in ("throughput", "p50", "p95", "p99"):
            values = [pair["delta_parallel_minus_no_parallel"][name] for pair in valid_pairs]
            metrics[name] = ({"median": statistics.median(values), "range": [min(values), max(values)],
                              "positive": sum(value > 0 for value in values), "negative": sum(value < 0 for value in values)}
                             if values else {"status": "NOT_AVAILABLE"})
        summary["workers"][worker_text] = {"valid_trial_count": len(valid_pairs), "excluded_trial_count": len(trials) - len(valid_pairs),
                                            "paired_differences": metrics}
    atomic_write_json(run_dir / "summary.json", summary)
    return summary


def validate_artifacts(run_dir: Path) -> dict[str, Any]:
    """Validate parseability, run IDs, schedule hashes, manifest hashes, and secret hygiene."""
    manifest = read_json(run_dir / "manifest.json")
    run_id = manifest["run_id"]
    schedule_path = run_dir / "query-schedule.json"
    order_path = run_dir / "execution-order.json"
    schedule = read_json(schedule_path)
    schedule_digest = verify_schedule(schedule)
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION or schedule.get("run_id") != run_id:
        errors.append("manifest/schedule schema or run-id mismatch")
    for key, path in (("query_schedule", schedule_path), ("execution_order", order_path)):
        recorded = manifest.get("artifacts", {}).get(key, {}).get("sha256")
        if recorded != file_sha256(path): errors.append(f"{key} file SHA mismatch")
    order = read_json(order_path)
    if order.get("run_id") != run_id or order != generate_execution_order(run_id, manifest["config"]["trials"]):
        errors.append("execution order mismatch")
    checked: list[str] = []
    forbidden = ("postgresql://", "postgres://", "password=", "dive_test_password")
    for path in sorted(run_dir.rglob("*.json")):
        value = read_json(path)
        checked.append(str(path.relative_to(run_dir)).replace("\\", "/"))
        if value.get("schema_version") and value.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"schema mismatch: {path.name}")
        if value.get("run_id") and value.get("run_id") != run_id:
            errors.append(f"run-id mismatch: {path.name}")
        lowered = path.read_text(encoding="utf-8").lower()
        if any(token in lowered for token in forbidden): errors.append(f"secret-like content: {path.name}")
    for worker_text, trials in manifest["checkpoints"].items():
        for trial_text, trial_record in trials.items():
            for arm in ARMS:
                record = trial_record[arm]
                path = arm_artifact_path(run_dir, int(worker_text), int(trial_text), arm)
                if record["status"] in {"PASS", "FAILED", "INVALID"}:
                    if not path.exists() or record.get("sha256") != file_sha256(path):
                        errors.append(f"arm artifact SHA mismatch: workers={worker_text} trial={trial_text} arm={arm}")
                    elif read_json(path).get("query_schedule_sha256") != schedule_digest:
                        errors.append(f"arm schedule SHA mismatch: workers={worker_text} trial={trial_text} arm={arm}")
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "PASS" if not errors else "FAIL",
            "query_schedule_sha256": schedule_digest, "json_files_checked": checked, "errors": errors,
            "secret_scan": "PASS" if not any("secret-like" in error for error in errors) else "FAIL"}


def run_paired_trial(run_dir: Path, dsn: str, container: str, workers: int, trial: int,
                     *, require_parallel_execution: bool = True) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    if workers not in manifest["config"]["worker_levels"] or trial not in manifest["config"]["trials"]:
        raise ValueError("worker/trial was not preregistered")
    ensure_environment(run_dir, dsn, container, expected_shm=512 * 1024 * 1024 if require_parallel_execution else None)
    order_doc = read_json(run_dir / "execution-order.json")
    if file_sha256(run_dir / "execution-order.json") != manifest["artifacts"]["execution_order"]["sha256"]:
        raise RuntimeError("execution-order artifact SHA mismatch")
    if order_doc != generate_execution_order(manifest["run_id"], manifest["config"]["trials"]):
        raise RuntimeError("execution-order content is not the preregistered counterbalance")
    order = next(row["order"] for row in order_doc["trials"] if row["trial_id"] == trial)
    artifacts = {arm: execute_trial_arm(run_dir, dsn, container, workers, trial, arm,
                                        require_parallel_execution=require_parallel_execution) for arm in order}
    pair = compare_pair(artifacts["parallel"], artifacts["no-parallel"])
    manifest = read_json(run_dir / "manifest.json")
    manifest["checkpoints"][str(workers)][str(trial)]["pair_status"] = pair["status"]
    atomic_write_json(run_dir / "manifest.json", manifest)
    aggregate_trials(run_dir)
    return {"workers": workers, "trial": trial, "execution_order": order, "pair": pair,
            "parallel_targets": artifacts["parallel"].get("timed_target_memory_ids"),
            "no_parallel_targets": artifacts["no-parallel"].get("timed_target_memory_ids")}


def run_smoke(dsn: str, container: str, smoke_root: Path) -> dict[str, Any]:
    import psycopg
    from psycopg import sql
    from .postgres_migrations import apply_migrations
    from .postgres_benchmark import BenchmarkEmbeddingProvider
    from .postgres_store import PostgresStore
    from . import postgres_scale_benchmark as scale
    run_id = "smoke-paired-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    schema = "paired_smoke_" + hashlib.sha256(run_id.encode()).hexdigest()[:16]
    run_dir = smoke_root / run_id
    try:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        with psycopg.connect(_schema_dsn(dsn, schema), autocommit=False) as migration:
            apply_migrations(migration, Path(__file__).parents[2] / "migrations")
        store = PostgresStore(_schema_dsn(dsn, schema), embedder=BenchmarkEmbeddingProvider())
        store.close()
        with connect(dsn, schema) as fixture:
            fixture.autocommit = False
            scale._bulk_insert(fixture, DEFAULT_NAMESPACE, 100, DATASET_SEED)
            fixture.commit()
        create_run(run_dir, run_id, dataset_size=100, worker_levels=(1,), trials=(1, 2),
                   operations_per_worker=3, warmups_per_worker=2, schema=schema)
        results = [run_paired_trial(run_dir, dsn, container, 1, trial, require_parallel_execution=False)
                   for trial in (1, 2)]
        proof = [{"trial": row["trial"], "parallel_targets": row["parallel_targets"],
                  "no_parallel_targets": row["no_parallel_targets"],
                  "equal": row["parallel_targets"] == row["no_parallel_targets"],
                  "pair_status": row["pair"]["status"]} for row in results]
        smoke = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
                 "status": "PASS" if all(row["equal"] and row["pair_status"] == "PASS" for row in proof) else "FAIL",
                 "fixture_rows": 100, "workers": 1, "trials": 2, "schedule_equality_proof": proof,
                 "temporary_schema_removed": True, "dsn_recorded": False}
        atomic_write_json(run_dir / "smoke-result.json", smoke)
        validation = validate_artifacts(run_dir)
        atomic_write_json(run_dir / "artifact-validation.json", validation)
        if validation["status"] != "PASS": raise RuntimeError(f"smoke artifact validation failed: {validation}")
        if smoke["status"] != "PASS": raise RuntimeError(f"smoke failed: {smoke}")
        return {**smoke, "run_dir": str(run_dir)}
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Paired PostgreSQL parallelism benchmark")
    parser.add_argument("--reports-root", type=Path, default=DEFAULT_REPORTS_ROOT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    sub = parser.add_subparsers(dest="command", required=True)
    trial_parser = sub.add_parser("paired-trial")
    trial_parser.add_argument("--run-id", required=True)
    trial_parser.add_argument("--workers", type=int, choices=WORKER_LEVELS, required=True)
    trial_parser.add_argument("--trial", type=int, choices=TRIALS, required=True)
    smoke_parser = sub.add_parser("smoke")
    smoke_parser.add_argument("--smoke-root", type=Path, default=Path(".pytest-postgres-parallelism-smoke"))
    args = parser.parse_args()
    import os
    dsn = os.environ.get("DIVE_TEST_POSTGRES_DSN")
    if not dsn: raise SystemExit("set DIVE_TEST_POSTGRES_DSN; it is never written to artifacts")
    if args.command == "smoke":
        result = run_smoke(dsn, args.container, args.smoke_root)
    else:
        run_dir = args.reports_root / args.run_id
        create_run(run_dir, args.run_id)
        result = run_paired_trial(run_dir, dsn, args.container, args.workers, args.trial)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
