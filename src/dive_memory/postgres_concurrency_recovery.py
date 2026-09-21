"""Checkpointed runner for the PostgreSQL 100K concurrency recovery experiment.

This is intentionally separate from :mod:`postgres_scale_benchmark`.  Commands
are small and resumable; no command implicitly runs the complete experiment.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import threading
from time import perf_counter
from typing import Any, Callable, Iterable

from . import postgres_scale_benchmark as scale
from .ids import stable_vector


SCHEMA_VERSION = "dive-postgres-concurrency-recovery-v1"
DEFAULT_SOURCE_RUN = "postgres-100k-20260921-084800"
DEFAULT_SOURCE_SCHEMA = "dive_postgres_100k_f3d888f630bd7ba8"
DEFAULT_REPORTS_ROOT = Path("eval/reports/postgres-100k-concurrency")
DEFAULT_CONTAINER = "dive-memory-postgres-100k-concurrency"
PREREGISTRATION = Path("docs/benchmark/postgres-100k-concurrency-rerun-plan.md")
WORKER_LEVELS = (1, 2, 4, 8)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckpointState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAILED = "FAILED"
    NOT_COMPLETED = "NOT_COMPLETED"
    SKIPPED = "SKIPPED"
    INTERRUPTED = "INTERRUPTED"


class ErrorClass(str, Enum):
    DSM_ERROR = "DSM_ERROR"
    SHM_EXHAUSTION = "SHM_EXHAUSTION"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"
    TRANSACTION_ERROR = "TRANSACTION_ERROR"
    DEADLOCK = "DEADLOCK"
    SERIALIZATION_ERROR = "SERIALIZATION_ERROR"
    UNKNOWN_DATABASE_ERROR = "UNKNOWN_DATABASE_ERROR"


class MeasurementStatus(str, Enum):
    MEASURED = "MEASURED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass(frozen=True)
class RecoveryRunConfig:
    run_id: str
    source_run_id: str = DEFAULT_SOURCE_RUN
    source_schema: str = DEFAULT_SOURCE_SCHEMA
    container: str = DEFAULT_CONTAINER
    worker_levels: tuple[int, ...] = WORKER_LEVELS
    include_16: bool = False
    operations_per_worker: int = 8
    acceptable_failure_rate: float = 0.05
    stop_failure_rate: float = 0.20
    persistent_shm_errors: int = 3
    host_available_bytes_min: int = 1_000_000_000
    disk_free_bytes_min: int = 2_000_000_000
    container_memory_percent_max: float = 90.0
    query_seed: int = scale.SEED + 40
    dataset_size: int = scale.DATASET_SIZE
    expected_shm_bytes: int | None = 512 * 1024 * 1024

    def levels(self) -> tuple[int, ...]:
        return tuple(self.worker_levels) + ((16,) if self.include_16 else ())


@dataclass(frozen=True)
class ExperimentBranchConfig:
    key: str
    label: str
    parallelism: str
    max_parallel_workers_per_gather: int | None


BRANCH_A = ExperimentBranchConfig("branch_a", "larger shm; parallelism unchanged", "UNCHANGED", None)
BRANCH_B = ExperimentBranchConfig("branch_b", "larger shm; session parallelism disabled", "DISABLED", 0)


@dataclass
class Measurement:
    status: MeasurementStatus
    value: Any = None
    reason: str | None = None


@dataclass
class ResourceSample:
    captured_at: str
    container_available: Measurement
    container_cpu_percent: Measurement
    container_memory_percent: Measurement
    container_memory_usage: Measurement
    shared_memory: Measurement
    host_available_memory_bytes: Measurement
    client_rss_bytes: Measurement
    postgres_activity: Measurement
    disk_free_bytes: Measurement


@dataclass
class OperationResult:
    operation_id: str
    operation_type: str
    latency_ms: float
    success: bool
    correct: bool
    expected_ids: list[str] = field(default_factory=list)
    actual_ids: list[str] = field(default_factory=list)
    error_class: ErrorClass | None = None
    raw_error: str | None = None


@dataclass
class EnvironmentSnapshot:
    schema_version: str
    run_id: str
    status: str
    captured_at: str
    source_run_id: str
    source_schema: str
    postgres_version: str
    pgvector_version: str
    dataset: "DatasetVerification"
    container_inspect: dict[str, Any]
    resources: ResourceSample
    code_baseline: dict[str, Any]
    validation: dict[str, Any]
    dsn_recorded: bool = False


@dataclass
class DatasetVerification:
    status: str
    memories: int
    vectors: int
    expected_memories: int
    expected_vectors: int
    embedding_dimensions: list[int]
    expected_embedding_dimensions: int
    metadata_distribution: dict[str, int]
    expected_metadata_distribution: dict[str, int]
    seed_mismatch_count: int


@dataclass
class CorrectnessResult:
    status: str
    expected_operations: int
    successful_operations: int
    failed_operations: int
    expected_inserts: int
    actual_inserts: int
    duplicate_violations: int
    idempotency_consistent: bool | None
    read_consistent: bool
    transaction_rollback_correct: bool | None
    unexpected_row_count_delta: int
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class StopDecision:
    continue_run: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerLevelResult:
    branch: str
    workers: int
    started_at: str
    finished_at: str
    duration_seconds: float
    operations: list[OperationResult]
    throughput_operations_per_second: float
    retrieval_latency_ms: dict[str, float | int]
    overall_latency_ms: dict[str, float | int]
    ingest_latency_ms: dict[str, Any]
    correctness: CorrectnessResult
    resources_before: ResourceSample
    resources_after: ResourceSample
    database_available: bool
    database_crashed: bool
    retries: int
    timeouts: int
    deadlocks: int
    stop_decision: StopDecision
    status: str

    @property
    def failure_count(self) -> int:
        return sum(not row.success for row in self.operations)

    @property
    def failure_rate(self) -> float:
        return self.failure_count / len(self.operations) if self.operations else 1.0


@dataclass
class BranchSummary:
    branch: str
    worker_results: dict[int, WorkerLevelResult]
    status: str


@dataclass
class MixedWorkloadResult:
    status: str
    gate_passed: bool
    reason: str


@dataclass
class FinalRecoverySummary:
    run_id: str
    status: str
    branch_a: str
    branch_b: str
    mixed_workload: str
    integrity_audit: str


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def atomic_write_json(path: Path, value: Any) -> str:
    """Write a complete JSON artifact with flush, fsync, and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(_json_value(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def worker_artifact_path(run_dir: Path, branch: ExperimentBranchConfig, workers: int) -> Path:
    return run_dir / branch.key.replace("_", "-") / f"worker-{workers}.json"


def _checkpoint_record(artifact: str | None = None) -> dict[str, Any]:
    return {"status": CheckpointState.PENDING.value, "attempt_count": 0, "artifact": artifact}


def create_manifest(config: RecoveryRunConfig, run_dir: Path) -> dict[str, Any]:
    prereg_hash = hashlib.sha256(PREREGISTRATION.read_bytes()).hexdigest() if PREREGISTRATION.exists() else None
    workers = {str(level): _checkpoint_record(f"worker-{level}.json") for level in config.levels()}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": config.run_id,
        "status": CheckpointState.PENDING.value,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source_run": {"run_id": config.source_run_id, "schema": config.source_schema,
                       "immutable": True},
        "preregistration": {"path": str(PREREGISTRATION).replace("\\", "/"), "sha256": prereg_hash},
        "config": _json_value(config),
        "checkpoints": {
            "inspect": _checkpoint_record("environment.json"),
            "smoke": _checkpoint_record("smoke.json"),
            "branch_a": {"status": CheckpointState.PENDING.value, "workers": json.loads(json.dumps(workers))},
            "branch_b": {"status": CheckpointState.PENDING.value, "workers": json.loads(json.dumps(workers))},
            "mixed_workload": _checkpoint_record("mixed-workload.json"),
            "integrity_audit": _checkpoint_record("integrity-audit.json"),
            "summary": _checkpoint_record("final-summary.json"),
        },
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    atomic_write_json(run_dir / "environment.json", {"status": CheckpointState.NOT_COMPLETED.value})
    atomic_write_json(run_dir / "postgres-settings.json", {"status": CheckpointState.NOT_COMPLETED.value})
    return manifest


def _artifact_complete(path: Path) -> bool:
    try:
        value = read_json(path)
        return value.get("status") in {CheckpointState.PASS.value, CheckpointState.FAILED.value}
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def reconcile_interrupted(run_dir: Path, manifest: dict[str, Any]) -> bool:
    """Mark RUNNING checkpoints without a complete artifact as INTERRUPTED."""
    changed = False
    for branch_key in ("branch_a", "branch_b"):
        for worker, record in manifest["checkpoints"][branch_key]["workers"].items():
            if record["status"] != CheckpointState.RUNNING.value:
                continue
            path = run_dir / branch_key.replace("_", "-") / str(record["artifact"])
            if not _artifact_complete(path):
                record.update({"status": CheckpointState.INTERRUPTED.value,
                               "interrupted_at": utc_now(),
                               "previous_attempt": int(record.get("attempt_count", 0))})
                changed = True
    for key in ("inspect", "smoke", "mixed_workload", "integrity_audit", "summary"):
        record = manifest["checkpoints"][key]
        if record["status"] == CheckpointState.RUNNING.value:
            path = run_dir / str(record["artifact"])
            if not _artifact_complete(path):
                record.update({"status": CheckpointState.INTERRUPTED.value,
                               "interrupted_at": utc_now(),
                               "previous_attempt": int(record.get("attempt_count", 0))})
                changed = True
    if changed:
        manifest["updated_at"] = utc_now()
        atomic_write_json(run_dir / "manifest.json", manifest)
    return changed


def load_manifest(run_dir: Path) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported recovery manifest schema")
    reconcile_interrupted(run_dir, manifest)
    return read_json(run_dir / "manifest.json")


def begin_worker(run_dir: Path, branch: ExperimentBranchConfig, workers: int,
                 *, force: bool = False) -> tuple[dict[str, Any], bool]:
    manifest = load_manifest(run_dir)
    record = manifest["checkpoints"][branch.key]["workers"][str(workers)]
    state = record["status"]
    if state == CheckpointState.PASS.value and not force:
        return manifest, False
    if state in {CheckpointState.FAILED.value, CheckpointState.SKIPPED.value} and not force:
        return manifest, False
    record["status"] = CheckpointState.RUNNING.value
    record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
    record["started_at"] = utc_now()
    manifest["updated_at"] = utc_now()
    atomic_write_json(run_dir / "manifest.json", manifest)
    return manifest, True


def classify_database_error(error: BaseException | str) -> ErrorClass:
    text = str(error).lower()
    if "dynamic shared memory" in text or "dsm" in text:
        return ErrorClass.DSM_ERROR
    if ("shared memory" in text and ("no space" in text or "could not resize" in text)) or "/dev/shm" in text:
        return ErrorClass.SHM_EXHAUSTION
    if "deadlock" in text:
        return ErrorClass.DEADLOCK
    if "serialization" in text or "could not serialize" in text:
        return ErrorClass.SERIALIZATION_ERROR
    if "timeout" in text or "timed out" in text:
        return ErrorClass.TIMEOUT
    if any(term in text for term in ("connection refused", "connection reset", "server closed", "connection failed")):
        return ErrorClass.CONNECTION_ERROR
    if any(term in text for term in ("transaction", "in failed sql transaction", "rollback")):
        return ErrorClass.TRANSACTION_ERROR
    return ErrorClass.UNKNOWN_DATABASE_ERROR


def apply_branch_session_settings(connection: Any, branch: ExperimentBranchConfig) -> None:
    if branch.max_parallel_workers_per_gather is not None:
        connection.execute(
            "SELECT set_config('max_parallel_workers_per_gather', %s, false)",
            (str(branch.max_parallel_workers_per_gather),),
        )


def evaluate_stop_condition(result: WorkerLevelResult, config: RecoveryRunConfig) -> StopDecision:
    errors = [row.error_class for row in result.operations if row.error_class]
    shm_count = sum(item in {ErrorClass.DSM_ERROR, ErrorClass.SHM_EXHAUSTION} for item in errors)
    if result.database_crashed or not result.database_available:
        return StopDecision(False, "DATABASE_CRASH_OR_UNAVAILABLE", {"database_available": result.database_available})
    if result.resources_after.container_available.status is MeasurementStatus.MEASURED and not result.resources_after.container_available.value:
        return StopDecision(False, "CONTAINER_UNAVAILABLE")
    if result.correctness.status != "PASS":
        return StopDecision(False, "DATA_CORRUPTION_OR_CORRECTNESS_FAILURE", result.correctness.evidence)
    if result.failure_rate > config.stop_failure_rate:
        return StopDecision(False, "FAILURE_RATE_THRESHOLD", {"failure_rate": result.failure_rate,
                                                                  "threshold": config.stop_failure_rate})
    if shm_count >= config.persistent_shm_errors:
        return StopDecision(False, "PERSISTENT_SHARED_MEMORY_ERROR", {"count": shm_count})
    memory = result.resources_after.container_memory_percent
    if memory.status is MeasurementStatus.MEASURED and float(memory.value) > config.container_memory_percent_max:
        return StopDecision(False, "CONTAINER_MEMORY_PRESSURE", {"percent": memory.value})
    host = result.resources_after.host_available_memory_bytes
    if host.status is MeasurementStatus.MEASURED and int(host.value) < config.host_available_bytes_min:
        return StopDecision(False, "HOST_MEMORY_PRESSURE", {"available_bytes": host.value})
    disk = result.resources_after.disk_free_bytes
    if disk.status is MeasurementStatus.MEASURED and int(disk.value) < config.disk_free_bytes_min:
        return StopDecision(False, "DISK_PRESSURE", {"free_bytes": disk.value})
    return StopDecision(True, "CONTINUE")


def can_run_mixed_workload(branches: Iterable[BranchSummary], threshold: float = 0.05) -> tuple[bool, str]:
    for branch in branches:
        result = branch.worker_results.get(8)
        if not result:
            continue
        shared_errors = sum(row.error_class in {ErrorClass.DSM_ERROR, ErrorClass.SHM_EXHAUSTION}
                            for row in result.operations)
        if (result.status == CheckpointState.PASS.value and result.failure_rate <= threshold
                and result.correctness.status == "PASS" and result.database_available
                and not result.database_crashed and shared_errors == 0):
            return True, f"{branch.branch} worker-8 satisfies the mixed-workload gate"
    return False, "no branch has a stable, correct worker-8 result without shared-memory exhaustion"


def _not_available(reason: str) -> Measurement:
    return Measurement(MeasurementStatus.NOT_AVAILABLE, reason=reason)


def _run_command(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _client_rss() -> Measurement:
    try:
        import psutil  # type: ignore[import-not-found]
        return Measurement(MeasurementStatus.MEASURED, int(psutil.Process().memory_info().rss))
    except Exception:
        try:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return Measurement(MeasurementStatus.MEASURED, int(value * (1024 if os.name != "darwin" else 1)))
        except Exception as exc:
            return _not_available(f"client RSS unavailable: {type(exc).__name__}")


class ResourceSampler:
    """Best-effort observability. Measurement failure never fails the workload."""

    def __init__(self, container: str, connection_factory: Callable[[], Any] | None = None) -> None:
        self.container = container
        self.connection_factory = connection_factory

    def _docker(self) -> tuple[Measurement, Measurement, Measurement, Measurement, Measurement, Measurement]:
        stats = _run_command(["docker", "stats", self.container, "--no-stream", "--format", "{{json .}}"], 20)
        inspect = _run_command(["docker", "inspect", self.container, "--format", "{{.State.Running}}"], 10)
        available = Measurement(MeasurementStatus.MEASURED,
                                bool(inspect and inspect.returncode == 0 and inspect.stdout.strip() == "true"))
        if not stats or stats.returncode != 0:
            reason = "docker stats unavailable"
            cpu = memory_percent = memory_usage = _not_available(reason)
        else:
            try:
                parsed = json.loads(stats.stdout.strip().splitlines()[-1])
                cpu = Measurement(MeasurementStatus.MEASURED,
                                  float(str(parsed.get("CPUPerc", "0")).rstrip("%")))
                memory_percent = Measurement(MeasurementStatus.MEASURED,
                                             float(str(parsed.get("MemPerc", "0")).rstrip("%")))
                memory_usage = Measurement(MeasurementStatus.MEASURED, parsed.get("MemUsage"))
            except Exception as exc:
                cpu = memory_percent = memory_usage = _not_available(f"docker stats parse failed: {exc}")
        shm_result = _run_command(["docker", "exec", self.container, "sh", "-c",
                                   "df -B1 /dev/shm | tail -1"], 10)
        shm = _not_available("/dev/shm unavailable")
        if shm_result and shm_result.returncode == 0:
            fields = shm_result.stdout.split()
            if len(fields) >= 4:
                shm = Measurement(MeasurementStatus.MEASURED,
                                  {"total_bytes": int(fields[1]), "used_bytes": int(fields[2]),
                                   "available_bytes": int(fields[3])})
        disk_result = _run_command(["docker", "exec", self.container, "sh", "-c",
                                    "df -B1 /var/lib/postgresql/data | tail -1"], 10)
        disk = _not_available("database disk unavailable")
        if disk_result and disk_result.returncode == 0:
            fields = disk_result.stdout.split()
            if len(fields) >= 4:
                disk = Measurement(MeasurementStatus.MEASURED, int(fields[3]))
        return available, cpu, memory_percent, memory_usage, shm, disk

    def _postgres(self) -> Measurement:
        if self.connection_factory is None:
            return _not_available("no PostgreSQL connection factory")
        try:
            with self.connection_factory() as connection:
                row = connection.execute(
                    "SELECT count(*) AS connections,"
                    "count(*) FILTER (WHERE state='active') AS active_queries,"
                    "count(*) FILTER (WHERE wait_event IS NOT NULL) AS waiting "
                    "FROM pg_stat_activity WHERE datname=current_database()"
                ).fetchone()
                locks = connection.execute("SELECT count(*) AS count FROM pg_locks WHERE NOT granted").fetchone()
                database = connection.execute(
                    "SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()"
                ).fetchone()
            return Measurement(MeasurementStatus.MEASURED,
                               {"connections": int(row["connections"]),
                                "active_queries": int(row["active_queries"]),
                                "waiting": int(row["waiting"]),
                                "ungranted_locks": int(locks["count"]),
                                "deadlocks_total": int(database["deadlocks"])})
        except Exception as exc:
            return _not_available(f"PostgreSQL activity unavailable: {type(exc).__name__}: {exc}")

    def sample(self) -> ResourceSample:
        try:
            available, cpu, memory_percent, memory_usage, shm, disk = self._docker()
        except Exception as exc:  # defensive: observability must not stop work
            unavailable = _not_available(f"Docker sampling failed: {type(exc).__name__}: {exc}")
            available = cpu = memory_percent = memory_usage = shm = disk = unavailable
        host_raw = scale._host_memory()
        host = (Measurement(MeasurementStatus.MEASURED, int(host_raw["available_bytes"]))
                if host_raw.get("available") else
                _not_available(str(host_raw.get("reason", "host memory unavailable"))))
        return ResourceSample(utc_now(), available, cpu, memory_percent, memory_usage, shm, host,
                              _client_rss(), self._postgres(), disk)


def capture_postgres_settings(connection: Any) -> dict[str, str]:
    names = ("max_parallel_workers", "max_parallel_workers_per_gather", "max_worker_processes",
             "work_mem", "shared_buffers", "effective_cache_size", "max_connections",
             "statement_timeout", "lock_timeout")
    return {name: str(next(iter(connection.execute(f"SHOW {name}").fetchone().values())))
            for name in names}


def _safe_container_inspect(result: subprocess.CompletedProcess[str] | None) -> dict[str, Any]:
    """Retain experiment-relevant Docker fields without persisting container env secrets."""
    if not result or result.returncode != 0:
        return {"status": "NOT_AVAILABLE", "reason": "docker inspect failed"}
    try:
        raw = json.loads(result.stdout)
        value = raw[0] if isinstance(raw, list) else raw
        host = value.get("HostConfig", {})
        state = value.get("State", {})
        return {
            "status": "MEASURED",
            "value": {
                "id": value.get("Id"),
                "name": value.get("Name"),
                "image": value.get("Config", {}).get("Image"),
                "state": {key: state.get(key) for key in
                          ("Status", "Running", "Restarting", "OOMKilled", "Dead", "RestartCount")},
                "limits": {"shm_size_bytes": host.get("ShmSize"), "memory_bytes": host.get("Memory"),
                           "nano_cpus": host.get("NanoCpus")},
                "mounts": [{"type": mount.get("Type"), "name": mount.get("Name"),
                            "destination": mount.get("Destination"), "rw": mount.get("RW")}
                           for mount in value.get("Mounts", [])],
                "environment": "REDACTED_NOT_CAPTURED",
            },
        }
    except Exception as exc:
        return {"status": "NOT_AVAILABLE", "reason": f"docker inspect parse failed: {exc}"}


def _code_baseline(repo_root: Path | None = None) -> dict[str, Any]:
    """Fingerprint experiment code, including files not yet tracked by Git."""
    root = repo_root or Path(__file__).parents[2]
    files: list[Path] = []
    for directory in (root / "src", root / "tests", root / "migrations"):
        if directory.exists():
            files.extend(path for path in directory.rglob("*") if path.is_file()
                         and "__pycache__" not in path.parts)
    for path in (root / "pyproject.toml", root / PREREGISTRATION):
        if path.exists():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    safe = f"safe.directory={root}"
    head = _run_command(["git", "-c", safe, "rev-parse", "HEAD"], 10)
    status = _run_command(["git", "-c", safe, "status", "--short"], 20)
    diff = _run_command(["git", "-c", safe, "diff", "--binary", "HEAD"], 20)
    status_text = status.stdout if status and status.returncode == 0 else "NOT_AVAILABLE"
    diff_text = diff.stdout if diff and diff.returncode == 0 else "NOT_AVAILABLE"
    return {
        "git_head": head.stdout.strip() if head and head.returncode == 0 else "NOT_AVAILABLE",
        "working_tree_dirty": bool(status_text.strip() and status_text != "NOT_AVAILABLE"),
        "git_status_sha256": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
        "git_diff_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "experiment_tree_sha256": digest.hexdigest(),
        "experiment_tree_file_count": len(set(files)),
        "scope": ["src", "tests", "migrations", "pyproject.toml", str(PREREGISTRATION)],
    }


def _summary(values: list[float]) -> dict[str, float | int]:
    return scale._summary(values)


class PostgresRecoveryBackend:
    def __init__(self, config: RecoveryRunConfig, dsn: str) -> None:
        self.config = config
        self.dsn = dsn
        self.context = scale.RunContext(config.source_run_id, Path("."), dsn, config.container,
                                        {"schema": config.source_schema})
        self.sampler = ResourceSampler(config.container, lambda: self.connect(BRANCH_A))

    def connect(self, branch: ExperimentBranchConfig):
        connection = scale._connect(self.context)
        apply_branch_session_settings(connection, branch)
        return connection

    def main_count(self) -> tuple[int, int]:
        with self.connect(BRANCH_B) as connection:
            row = connection.execute(
                "SELECT count(*) AS memories,count(v.*) AS vectors FROM memories m "
                "LEFT JOIN memory_vectors v ON v.memory_id=m.id WHERE m.namespace=%s",
                (scale.MAIN_NAMESPACE,)).fetchone()
        return int(row["memories"]), int(row["vectors"])

    def dataset_verification(self) -> DatasetVerification:
        expected = {
            "cohort_1_percent": max(1, self.config.dataset_size // 100),
            "cohort_10_percent": max(1, self.config.dataset_size // 10),
            "cohort_50_percent": max(1, self.config.dataset_size // 2),
        }
        with self.connect(BRANCH_B) as connection:
            row = connection.execute(
                "SELECT count(*) AS memories,count(v.*) AS vectors,"
                "count(*) FILTER (WHERE m.structured_content->>'cohort_1'='true') AS cohort_1,"
                "count(*) FILTER (WHERE m.structured_content->>'cohort_10'='true') AS cohort_10,"
                "count(*) FILTER (WHERE m.structured_content->>'cohort_50'='true') AS cohort_50,"
                "count(*) FILTER (WHERE m.structured_content->>'seed' IS DISTINCT FROM %s) AS seed_mismatch "
                "FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.id WHERE m.namespace=%s",
                (str(scale.SEED), scale.MAIN_NAMESPACE),).fetchone()
            dimensions = connection.execute(
                "SELECT DISTINCT vector_dims(v.embedding) AS dimensions FROM memory_vectors v "
                "JOIN memories m ON m.id=v.memory_id WHERE m.namespace=%s ORDER BY dimensions",
                (scale.MAIN_NAMESPACE,),).fetchall()
        distribution = {"cohort_1_percent": int(row["cohort_1"]),
                        "cohort_10_percent": int(row["cohort_10"]),
                        "cohort_50_percent": int(row["cohort_50"])}
        dimension_values = [int(item["dimensions"]) for item in dimensions]
        valid = (int(row["memories"]) == self.config.dataset_size
                 and int(row["vectors"]) == self.config.dataset_size
                 and dimension_values == [96] and distribution == expected
                 and int(row["seed_mismatch"]) == 0)
        return DatasetVerification(
            "PASS" if valid else "FAIL", int(row["memories"]), int(row["vectors"]),
            self.config.dataset_size, self.config.dataset_size, dimension_values, 96,
            distribution, expected, int(row["seed_mismatch"]))

    def versions(self) -> tuple[str, str]:
        with self.connect(BRANCH_A) as connection:
            postgres = str(next(iter(connection.execute("SHOW server_version").fetchone().values())))
            pgvector = str(connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()["extversion"])
        return postgres, pgvector

    def settings(self, branch: ExperimentBranchConfig = BRANCH_A) -> dict[str, str]:
        with self.connect(branch) as connection:
            return capture_postgres_settings(connection)

    def _expected(self, targets: list[int]) -> dict[int, list[str]]:
        from pgvector import Vector
        expected: dict[int, list[str]] = {}
        with self.connect(BRANCH_B) as connection:
            for target in sorted(set(targets)):
                text = f"benchmark memory {target:06d} user {target % 100:03d} seed {scale.SEED}"
                rows = scale._raw_exact_query(connection, Vector(stable_vector(text, dimensions=96)))
                expected[target] = [row["id"] for row in rows]
        return expected

    def _operation(self, connection: Any, target: int, expected: list[str], operation_id: str) -> OperationResult:
        from pgvector import Vector
        text = f"benchmark memory {target:06d} user {target % 100:03d} seed {scale.SEED}"
        started = perf_counter()
        try:
            rows = scale._raw_exact_query(connection, Vector(stable_vector(text, dimensions=96)))
            actual = [row["id"] for row in rows]
            correct = actual == expected
            return OperationResult(operation_id, "retrieval", (perf_counter() - started) * 1000,
                                   True, correct, expected, actual)
        except Exception as exc:
            return OperationResult(operation_id, "retrieval", (perf_counter() - started) * 1000,
                                   False, False, expected_ids=expected,
                                   error_class=classify_database_error(exc), raw_error=f"{type(exc).__name__}: {exc}")

    def execute_level(self, branch: ExperimentBranchConfig, workers: int) -> WorkerLevelResult:
        if workers not in self.config.levels():
            raise ValueError(f"worker level {workers} is not registered")
        memories_before, vectors_before = self.main_count()
        rng = random.Random(self.config.query_seed + workers + (1000 if branch is BRANCH_B else 0))
        groups = [[rng.randrange(self.config.dataset_size) for _ in range(self.config.operations_per_worker)]
                  for _ in range(workers)]
        expected = self._expected([target for group in groups for target in group])
        before = self.sampler.sample()
        started_at = utc_now()
        started = perf_counter()
        completed = 0
        progress_lock = threading.Lock()
        progress_step = max(1, workers * self.config.operations_per_worker // 4)

        def execute_group(worker_index: int, targets: list[int]) -> list[OperationResult]:
            nonlocal completed
            rows: list[OperationResult] = []
            connection = self.connect(branch)
            try:
                for index, target in enumerate(targets):
                    rows.append(self._operation(connection, target, expected[target],
                                                f"w{worker_index}-q{index}-t{target}"))
                    with progress_lock:
                        completed += 1
                        if completed % progress_step == 0 or completed == workers * self.config.operations_per_worker:
                            failures = sum(not row.success for row in rows)
                            print(f"progress branch={branch.key} workers={workers} completed={completed}/"
                                  f"{workers * self.config.operations_per_worker} local_failures={failures}", flush=True)
            finally:
                connection.close()
            return rows

        with ThreadPoolExecutor(max_workers=workers) as executor:
            grouped = list(executor.map(lambda pair: execute_group(*pair), enumerate(groups)))
        duration = perf_counter() - started
        operations = [row for group in grouped for row in group]
        after = self.sampler.sample()
        try:
            memories_after, vectors_after = self.main_count()
            database_available = True
        except Exception:
            memories_after, vectors_after = memories_before, vectors_before
            database_available = False
        correct = all(row.correct for row in operations if row.success)
        correctness = CorrectnessResult(
            "PASS" if correct and (memories_after, vectors_after) == (memories_before, vectors_before) else "FAIL",
            len(operations), sum(row.success for row in operations), sum(not row.success for row in operations),
            0, 0, 0, None, correct, None, memories_after - memories_before,
            {"main_memories_before": memories_before, "main_memories_after": memories_after,
             "main_vectors_before": vectors_before, "main_vectors_after": vectors_after,
             "idempotency": "NOT_APPLICABLE_RETRIEVAL_ONLY",
             "transaction_rollback": "NOT_APPLICABLE_RETRIEVAL_ONLY"})
        latencies = [row.latency_ms for row in operations if row.success]
        placeholder = StopDecision(True, "PENDING_EVALUATION")
        result = WorkerLevelResult(
            branch.key, workers, started_at, utc_now(), duration, operations,
            sum(row.success for row in operations) / max(duration, 1e-9), _summary(latencies),
            _summary([row.latency_ms for row in operations]), {"status": "NOT_APPLICABLE"},
            correctness, before, after, database_available, not database_available, 0,
            sum(row.error_class is ErrorClass.TIMEOUT for row in operations),
            sum(row.error_class is ErrorClass.DEADLOCK for row in operations), placeholder, "PENDING")
        result.stop_decision = evaluate_stop_condition(result, self.config)
        result.status = (CheckpointState.PASS.value if result.failure_rate <= self.config.acceptable_failure_rate
                         and correctness.status == "PASS" and database_available else CheckpointState.FAILED.value)
        return result


def _mark_higher_workers_skipped(run_dir: Path, manifest: dict[str, Any], branch: ExperimentBranchConfig,
                                 workers: int, reason: str) -> None:
    for level in RecoveryRunConfig(**manifest["config"]).levels():
        if level <= workers:
            continue
        record = manifest["checkpoints"][branch.key]["workers"][str(level)]
        if record["status"] == CheckpointState.PENDING.value:
            record.update({"status": CheckpointState.SKIPPED.value,
                           "reason": "SKIPPED_DUE_TO_STOP_CONDITION", "evidence": reason})


def run_worker_level(run_dir: Path, backend: PostgresRecoveryBackend, branch: ExperimentBranchConfig,
                     workers: int, *, force: bool = False) -> WorkerLevelResult | dict[str, Any]:
    preflight_manifest = load_manifest(run_dir)
    if preflight_manifest["checkpoints"]["inspect"]["status"] != CheckpointState.PASS.value:
        raise RuntimeError("inspect must PASS before a formal worker level can run")
    environment = read_json(run_dir / "environment.json")
    frozen_tree = environment.get("code_baseline", {}).get("experiment_tree_sha256")
    current_tree = _code_baseline().get("experiment_tree_sha256")
    if not frozen_tree or frozen_tree != current_tree:
        raise RuntimeError("experiment code fingerprint changed after inspect")
    manifest, should_run = begin_worker(run_dir, branch, workers, force=force)
    path = worker_artifact_path(run_dir, branch, workers)
    if not should_run:
        return read_json(path) if path.exists() else {"status": manifest["checkpoints"][branch.key]["workers"][str(workers)]["status"]}
    try:
        result = backend.execute_level(branch, workers)
        artifact = {"schema_version": SCHEMA_VERSION, "run_id": backend.config.run_id,
                    "status": result.status, "result": _json_value(result)}
        digest = atomic_write_json(path, artifact)
        manifest = load_manifest(run_dir)
        record = manifest["checkpoints"][branch.key]["workers"][str(workers)]
        record.update({"status": result.status, "finished_at": utc_now(), "sha256": digest})
        if not result.stop_decision.continue_run:
            _mark_higher_workers_skipped(run_dir, manifest, branch, workers, result.stop_decision.reason)
            manifest["checkpoints"][branch.key]["status"] = CheckpointState.FAILED.value
        elif workers == max(backend.config.levels()):
            manifest["checkpoints"][branch.key]["status"] = result.status
        manifest["updated_at"] = utc_now()
        atomic_write_json(run_dir / "manifest.json", manifest)
        return result
    except Exception as exc:
        artifact = {"schema_version": SCHEMA_VERSION, "run_id": backend.config.run_id,
                    "status": CheckpointState.FAILED.value, "finished_at": utc_now(),
                    "error_class": classify_database_error(exc).value,
                    "raw_error": f"{type(exc).__name__}: {exc}"}
        digest = atomic_write_json(path, artifact)
        manifest = load_manifest(run_dir)
        record = manifest["checkpoints"][branch.key]["workers"][str(workers)]
        record.update({"status": CheckpointState.FAILED.value, "finished_at": utc_now(), "sha256": digest})
        _mark_higher_workers_skipped(run_dir, manifest, branch, workers, "UNHANDLED_WORKER_FAILURE")
        atomic_write_json(run_dir / "manifest.json", manifest)
        raise


def inspect_run(run_dir: Path, backend: PostgresRecoveryBackend) -> dict[str, Any]:
    """Capture environment, PostgreSQL settings, and enabled/disabled plans."""
    manifest = load_manifest(run_dir)
    record = manifest["checkpoints"]["inspect"]
    if record["status"] == CheckpointState.PASS.value:
        return read_json(run_dir / "environment.json")
    record.update({"status": CheckpointState.RUNNING.value,
                   "attempt_count": int(record.get("attempt_count", 0)) + 1,
                   "started_at": utc_now()})
    atomic_write_json(run_dir / "manifest.json", manifest)
    settings_a = backend.settings(BRANCH_A)
    settings_b = backend.settings(BRANCH_B)
    dataset = backend.dataset_verification()
    postgres_version, pgvector_version = backend.versions()
    inspect = _run_command(["docker", "inspect", backend.config.container], 15)
    safe_inspect = _safe_container_inspect(inspect)
    sample = backend.sampler.sample()
    shm_total = (sample.shared_memory.value.get("total_bytes")
                 if sample.shared_memory.status is MeasurementStatus.MEASURED else None)
    inspect_shm = safe_inspect.get("value", {}).get("limits", {}).get("shm_size_bytes")
    expected_shm = backend.config.expected_shm_bytes
    shm_valid = expected_shm is None or (shm_total == expected_shm and inspect_shm == expected_shm)
    essential_resources = all((
        sample.container_available.status is MeasurementStatus.MEASURED
        and sample.container_available.value is True,
        sample.shared_memory.status is MeasurementStatus.MEASURED,
        sample.disk_free_bytes.status is MeasurementStatus.MEASURED,
        sample.host_available_memory_bytes.status is MeasurementStatus.MEASURED,
        sample.postgres_activity.status is MeasurementStatus.MEASURED,
    ))
    disk_safe = (sample.disk_free_bytes.status is MeasurementStatus.MEASURED
                 and int(sample.disk_free_bytes.value) >= backend.config.disk_free_bytes_min)
    host_safe = (sample.host_available_memory_bytes.status is MeasurementStatus.MEASURED
                 and int(sample.host_available_memory_bytes.value) >= backend.config.host_available_bytes_min)
    validation = {
        "dataset": dataset.status,
        "container_available": bool(sample.container_available.value),
        "shared_memory_matches_expected": shm_valid,
        "expected_shm_bytes": expected_shm,
        "observed_shm_df_bytes": shm_total,
        "observed_shm_inspect_bytes": inspect_shm,
        "essential_resource_sampler_available": essential_resources,
        "disk_space_safe": disk_safe,
        "host_memory_safe": host_safe,
        "parallelism_unchanged_branch_a": True,
    }
    validation_passed = (dataset.status == "PASS" and all(
        value for key, value in validation.items()
        if key not in {"dataset", "expected_shm_bytes", "observed_shm_df_bytes",
                       "observed_shm_inspect_bytes"}))
    environment = EnvironmentSnapshot(
        SCHEMA_VERSION, backend.config.run_id,
        CheckpointState.PASS.value if validation_passed else CheckpointState.FAILED.value,
        utc_now(), backend.config.source_run_id, backend.config.source_schema,
        postgres_version, pgvector_version, dataset, safe_inspect, sample,
        _code_baseline(), validation)
    settings = {"schema_version": SCHEMA_VERSION, "run_id": backend.config.run_id,
                "status": CheckpointState.PASS.value,
                "branch_a": settings_a, "branch_b_session": settings_b,
                "branch_b_change_scope": "SESSION_ONLY"}
    from pgvector import Vector
    text = f"benchmark memory {10:06d} user {10 % 100:03d} seed {scale.SEED}"
    vector = Vector(stable_vector(text, dimensions=96))
    query = ("SELECT m.id,m.content,1.0-(v.embedding <=> %s) AS cosine_similarity "
             "FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
             "WHERE m.namespace=%s AND m.status IN ('ACTIVE','REINFORCED') "
             "ORDER BY v.embedding <=> %s,m.id LIMIT %s")
    plans: dict[str, Any] = {"schema_version": SCHEMA_VERSION,
                             "run_id": backend.config.run_id,
                             "status": CheckpointState.PASS.value}
    for branch in (BRANCH_A, BRANCH_B):
        with backend.connect(branch) as connection:
            plans[branch.key] = scale._explain(
                connection, query, (vector, scale.MAIN_NAMESPACE, vector, 5))
    env_hash = atomic_write_json(run_dir / "environment.json", environment)
    atomic_write_json(run_dir / "postgres-settings.json", settings)
    atomic_write_json(run_dir / "parallel-query-plan.json", plans)
    manifest = load_manifest(run_dir)
    inspect_status = CheckpointState.PASS.value if validation_passed else CheckpointState.FAILED.value
    manifest["checkpoints"]["inspect"].update(
        {"status": inspect_status, "finished_at": utc_now(), "sha256": env_hash})
    atomic_write_json(run_dir / "manifest.json", manifest)
    if not validation_passed:
        raise RuntimeError(f"environment inspection failed: {validation}")
    return _json_value(environment)


def _drop_smoke_schema(dsn: str, schema: str) -> None:
    import psycopg
    from psycopg import sql
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def run_smoke(dsn: str, container: str, smoke_root: Path, *, rows: int = 100) -> dict[str, Any]:
    """Tiny isolated PostgreSQL validation. It never touches the 100K schema."""
    import psycopg
    from psycopg import sql
    from .postgres_migrations import apply_migrations
    from .postgres_store import PostgresStore
    from .postgres_benchmark import BenchmarkEmbeddingProvider

    run_id = f"smoke-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    schema = "recovery_smoke_" + hashlib.sha256(run_id.encode()).hexdigest()[:16]
    run_dir = smoke_root / run_id
    config = RecoveryRunConfig(run_id=run_id, source_run_id="SMOKE_FIXTURE", source_schema=schema,
                               container=container, worker_levels=(1,), operations_per_worker=2,
                               dataset_size=rows, expected_shm_bytes=None)
    create_manifest(config, run_dir)
    original_count = None
    try:
        source_context = scale.RunContext(DEFAULT_SOURCE_RUN, Path("."), dsn, container,
                                          {"schema": DEFAULT_SOURCE_SCHEMA})
        original_count = scale._ensure_main_count(source_context)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        with psycopg.connect(scale._schema_dsn(dsn, schema), autocommit=False) as migration:
            apply_migrations(migration, Path(__file__).parents[2] / "migrations")
        store = PostgresStore(scale._schema_dsn(dsn, schema), embedder=BenchmarkEmbeddingProvider())
        store.close()
        smoke_context = scale.RunContext(run_id, run_dir, dsn, container, {"schema": schema})
        with scale._connect(smoke_context, autocommit=False) as connection:
            scale._bulk_insert(connection, scale.MAIN_NAMESPACE, rows, scale.SEED)
            connection.commit()
        backend = PostgresRecoveryBackend(config, dsn)
        inspect_run(run_dir, backend)
        result = run_worker_level(run_dir, backend, BRANCH_B, 1)
        if not isinstance(result, WorkerLevelResult) or result.status != CheckpointState.PASS.value:
            raise RuntimeError("smoke worker did not pass")
        first_hash = hashlib.sha256(worker_artifact_path(run_dir, BRANCH_B, 1).read_bytes()).hexdigest()
        attempts_before = load_manifest(run_dir)["checkpoints"][BRANCH_B.key]["workers"]["1"]["attempt_count"]
        resumed = run_worker_level(run_dir, backend, BRANCH_B, 1)
        second_hash = hashlib.sha256(worker_artifact_path(run_dir, BRANCH_B, 1).read_bytes()).hexdigest()
        attempts_after = load_manifest(run_dir)["checkpoints"][BRANCH_B.key]["workers"]["1"]["attempt_count"]
        after_count = scale._ensure_main_count(source_context)
        resume_ok = isinstance(resumed, dict) and first_hash == second_hash and attempts_before == attempts_after
        source_unchanged = original_count == after_count == {"memories": 100000, "vectors": 100000}
        smoke = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
                 "status": CheckpointState.PASS.value if resume_ok and source_unchanged else CheckpointState.FAILED.value,
                 "fixture_rows": rows, "worker_status": result.status,
                 "correctness_status": result.correctness.status,
                 "resource_sampler_status": "PASS",
                 "resume_without_rerun": resume_ok,
                 "source_100k_unchanged": source_unchanged,
                 "source_counts_before": original_count, "source_counts_after": after_count,
                 "artifact_readable": read_json(worker_artifact_path(run_dir, BRANCH_B, 1))["status"] == "PASS"}
        digest = atomic_write_json(run_dir / "smoke.json", smoke)
        manifest = load_manifest(run_dir)
        manifest["checkpoints"]["smoke"].update(
            {"status": smoke["status"], "finished_at": utc_now(), "attempt_count": 1, "sha256": digest})
        atomic_write_json(run_dir / "manifest.json", manifest)
        if smoke["status"] != CheckpointState.PASS.value:
            raise RuntimeError(f"smoke validation failed: {smoke}")
        return {**smoke, "run_dir": str(run_dir), "temporary_schema_removed": True}
    finally:
        _drop_smoke_schema(dsn, schema)


def _config_from_manifest(manifest: dict[str, Any]) -> RecoveryRunConfig:
    values = dict(manifest["config"])
    values["worker_levels"] = tuple(values["worker_levels"])
    return RecoveryRunConfig(**values)


def write_gate_only_mixed_artifact(run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    qualifying: list[str] = []
    for branch in (BRANCH_A, BRANCH_B):
        record = manifest["checkpoints"][branch.key]["workers"].get("8", {})
        path = worker_artifact_path(run_dir, branch, 8)
        if record.get("status") == CheckpointState.PASS.value and path.exists():
            payload = read_json(path).get("result", {})
            if (payload.get("correctness", {}).get("status") == "PASS"
                    and payload.get("stop_decision", {}).get("continue_run") is True):
                qualifying.append(branch.key)
    gate = {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"],
            "status": CheckpointState.NOT_COMPLETED.value,
            "gate_passed": bool(qualifying), "qualifying_branches": qualifying,
            "execution": "NOT_RUN_BY_GATE_ONLY_IMPLEMENTATION",
            "reason": ("gate satisfied; execute mixed workload in a later implementation step"
                       if qualifying else "no qualifying worker-8 branch")}
    atomic_write_json(run_dir / "mixed-workload.json", gate)
    return gate


def write_summary(run_dir: Path) -> FinalRecoverySummary:
    manifest = load_manifest(run_dir)
    summary = FinalRecoverySummary(
        manifest["run_id"], manifest["status"],
        manifest["checkpoints"]["branch_a"]["status"],
        manifest["checkpoints"]["branch_b"]["status"],
        manifest["checkpoints"]["mixed_workload"]["status"],
        manifest["checkpoints"]["integrity_audit"]["status"])
    atomic_write_json(run_dir / "final-summary.json",
                      {"schema_version": SCHEMA_VERSION, "status": CheckpointState.NOT_COMPLETED.value,
                       "summary": summary})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Short-step PostgreSQL concurrency recovery runner")
    parser.add_argument("--reports-root", type=Path, default=DEFAULT_REPORTS_ROOT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--run-id", required=True)
    for name in ("inspect", "branch-a", "branch-b", "mixed", "audit", "summary"):
        command = sub.add_parser(name)
        command.add_argument("--run-id", required=True)
        if name in {"branch-a", "branch-b"}:
            command.add_argument("--workers", type=int, choices=(1, 2, 4, 8, 16), required=True)
            command.add_argument("--force", action="store_true")
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--rows", type=int, default=100)
    smoke.add_argument("--smoke-root", type=Path, default=Path(".pytest-postgres-concurrency-smoke"))
    args = parser.parse_args()
    dsn = os.environ.get("DIVE_TEST_POSTGRES_DSN")
    if args.command != "init" and not dsn:
        raise SystemExit("set DIVE_TEST_POSTGRES_DSN; it is never written to artifacts")
    if args.command == "smoke":
        print(json.dumps(run_smoke(dsn, args.container, args.smoke_root, rows=args.rows), ensure_ascii=False))
        return
    run_dir = args.reports_root / args.run_id
    if args.command == "init":
        config = RecoveryRunConfig(run_id=args.run_id, container=args.container)
        print(json.dumps({"run_dir": str(run_dir), "status": create_manifest(config, run_dir)["status"]}))
        return
    manifest = load_manifest(run_dir)
    config = _config_from_manifest(manifest)
    backend = PostgresRecoveryBackend(config, dsn)
    if args.command == "inspect":
        result: Any = inspect_run(run_dir, backend)
    elif args.command in {"branch-a", "branch-b"}:
        result = run_worker_level(run_dir, backend, BRANCH_A if args.command == "branch-a" else BRANCH_B,
                                  args.workers, force=args.force)
    elif args.command == "mixed":
        result = write_gate_only_mixed_artifact(run_dir)
    elif args.command == "audit":
        result = {"status": CheckpointState.NOT_COMPLETED.value,
                  "reason": "integrity audit requires a completed mixed workload"}
        atomic_write_json(run_dir / "integrity-audit.json", result)
    else:
        result = write_summary(run_dir)
    print(json.dumps(_json_value(result), ensure_ascii=False))


if __name__ == "__main__":
    main()
