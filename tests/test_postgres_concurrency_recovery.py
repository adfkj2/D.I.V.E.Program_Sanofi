import json

from dive_memory import postgres_concurrency_recovery as recovery


def _measurement(value=None, *, available=True):
    return recovery.Measurement(
        recovery.MeasurementStatus.MEASURED if available else recovery.MeasurementStatus.NOT_AVAILABLE,
        value=value,
        reason=None if available else "fixture unavailable",
    )


def _resources(*, container=True, memory=10.0, disk=10_000_000_000):
    return recovery.ResourceSample(
        "2026-09-21T00:00:00+00:00",
        _measurement(container), _measurement(1.0), _measurement(memory), _measurement("10MiB / 1GiB"),
        _measurement({"total_bytes": 512 * 1024 * 1024, "used_bytes": 1024}),
        _measurement(5_000_000_000), _measurement(10_000_000),
        _measurement({"connections": 1}), _measurement(disk),
    )


def _operation(index=0, *, success=True, correct=True, error_class=None):
    return recovery.OperationResult(
        f"op-{index}", "retrieval", 10.0, success, correct,
        expected_ids=["a"], actual_ids=["a"] if correct else ["b"],
        error_class=error_class, raw_error="fixture" if error_class else None,
    )


def _result(operations=None, *, correctness="PASS", database=True):
    operations = operations or [_operation()]
    oracle = recovery.CorrectnessResult(
        correctness, len(operations), sum(row.success for row in operations),
        sum(not row.success for row in operations), 0, 0, 0, True,
        correctness == "PASS", True, 0, {"fixture": True},
    )
    return recovery.WorkerLevelResult(
        "branch_a", 8, "start", "finish", 1.0, operations,
        float(sum(row.success for row in operations)), recovery._summary([10.0]),
        recovery._summary([10.0]), {"status": "NOT_APPLICABLE"}, oracle,
        _resources(), _resources(), database, not database, 0, 0, 0,
        recovery.StopDecision(True, "PENDING"), recovery.CheckpointState.PASS.value,
    )


def test_manifest_creation_prepares_independent_artifacts(tmp_path):
    config = recovery.RecoveryRunConfig("recovery-fixture")
    manifest = recovery.create_manifest(config, tmp_path)

    assert manifest["source_run"]["immutable"] is True
    assert (tmp_path / "manifest.json").exists()
    assert recovery.read_json(tmp_path / "environment.json")["status"] == "NOT_COMPLETED"
    assert recovery.read_json(tmp_path / "postgres-settings.json")["status"] == "NOT_COMPLETED"
    assert set(manifest["checkpoints"]["branch_a"]["workers"]) == {"1", "2", "4", "8"}


def test_atomic_json_writer_round_trips_without_temporary_file(tmp_path):
    path = tmp_path / "artifact.json"
    digest = recovery.atomic_write_json(path, {"status": "PASS", "text": "中文"})

    assert json.loads(path.read_text(encoding="utf-8"))["text"] == "中文"
    assert len(digest) == 64
    assert list(tmp_path.glob("*.tmp")) == []


def test_resume_from_pass_does_not_rerun_without_force(tmp_path):
    config = recovery.RecoveryRunConfig("resume-pass")
    manifest = recovery.create_manifest(config, tmp_path)
    artifact = recovery.worker_artifact_path(tmp_path, recovery.BRANCH_A, 1)
    recovery.atomic_write_json(artifact, {"status": "PASS"})
    manifest["checkpoints"]["branch_a"]["workers"]["1"].update(
        {"status": "PASS", "attempt_count": 1})
    recovery.atomic_write_json(tmp_path / "manifest.json", manifest)

    loaded, should_run = recovery.begin_worker(tmp_path, recovery.BRANCH_A, 1)

    assert should_run is False
    assert loaded["checkpoints"]["branch_a"]["workers"]["1"]["attempt_count"] == 1


def test_resume_from_running_without_artifact_becomes_interrupted(tmp_path):
    config = recovery.RecoveryRunConfig("resume-interrupted")
    manifest = recovery.create_manifest(config, tmp_path)
    record = manifest["checkpoints"]["branch_b"]["workers"]["2"]
    record.update({"status": "RUNNING", "attempt_count": 2})
    recovery.atomic_write_json(tmp_path / "manifest.json", manifest)

    loaded = recovery.load_manifest(tmp_path)

    record = loaded["checkpoints"]["branch_b"]["workers"]["2"]
    assert record["status"] == "INTERRUPTED"
    assert record["previous_attempt"] == 2
    assert "interrupted_at" in record


def test_failure_rate_threshold_stops_higher_concurrency():
    result = _result([_operation(0), _operation(1, success=False, correct=False,
                                                     error_class=recovery.ErrorClass.UNKNOWN_DATABASE_ERROR)])
    decision = recovery.evaluate_stop_condition(result, recovery.RecoveryRunConfig("failure-rate"))

    assert decision.continue_run is False
    assert decision.reason == "FAILURE_RATE_THRESHOLD"


def test_correctness_failure_stops_even_with_zero_transport_failures():
    result = _result([_operation()], correctness="FAIL")
    decision = recovery.evaluate_stop_condition(result, recovery.RecoveryRunConfig("correctness"))

    assert result.failure_rate == 0.0
    assert decision.reason == "DATA_CORRUPTION_OR_CORRECTNESS_FAILURE"


def test_persistent_shared_memory_errors_stop():
    errors = [_operation(index, success=False, correct=False,
                         error_class=recovery.ErrorClass.DSM_ERROR) for index in range(3)]
    result = _result(errors)
    result.correctness.status = "PASS"
    decision = recovery.evaluate_stop_condition(result, recovery.RecoveryRunConfig("shm", stop_failure_rate=1.0))

    assert decision.reason == "PERSISTENT_SHARED_MEMORY_ERROR"


def test_error_classification_covers_required_database_classes():
    cases = {
        "could not resize dynamic shared memory segment": recovery.ErrorClass.DSM_ERROR,
        "could not resize shared memory segment: No space left on device": recovery.ErrorClass.SHM_EXHAUSTION,
        "server closed the connection unexpectedly": recovery.ErrorClass.CONNECTION_ERROR,
        "statement timeout": recovery.ErrorClass.TIMEOUT,
        "transaction is aborted": recovery.ErrorClass.TRANSACTION_ERROR,
        "deadlock detected": recovery.ErrorClass.DEADLOCK,
        "could not serialize access": recovery.ErrorClass.SERIALIZATION_ERROR,
        "database exploded oddly": recovery.ErrorClass.UNKNOWN_DATABASE_ERROR,
    }
    assert {text: recovery.classify_database_error(text) for text in cases} == cases


def test_branch_configuration_and_parallel_off_are_session_scoped():
    class FakeConnection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))

    connection = FakeConnection()
    recovery.apply_branch_session_settings(connection, recovery.BRANCH_A)
    assert connection.calls == []
    recovery.apply_branch_session_settings(connection, recovery.BRANCH_B)
    assert connection.calls == [(
        "SELECT set_config('max_parallel_workers_per_gather', %s, false)", ("0",)
    )]
    assert recovery.BRANCH_B.parallelism == "DISABLED"


def test_worker_artifact_naming_is_branch_and_level_specific(tmp_path):
    assert recovery.worker_artifact_path(tmp_path, recovery.BRANCH_A, 4) == (
        tmp_path / "branch-a" / "worker-4.json")
    assert recovery.worker_artifact_path(tmp_path, recovery.BRANCH_B, 8) == (
        tmp_path / "branch-b" / "worker-8.json")


def test_mixed_workload_gate_requires_stable_worker_eight():
    passing = _result([_operation()])
    branch = recovery.BranchSummary("branch_a", {8: passing}, "PASS")
    allowed, reason = recovery.can_run_mixed_workload([branch])
    assert allowed is True
    assert "branch_a" in reason

    passing.operations[0].error_class = recovery.ErrorClass.DSM_ERROR
    allowed, _ = recovery.can_run_mixed_workload([branch])
    assert allowed is False


def test_include_16_requires_explicit_configuration():
    assert recovery.RecoveryRunConfig("default").levels() == (1, 2, 4, 8)
    assert recovery.RecoveryRunConfig("explicit", include_16=True).levels() == (1, 2, 4, 8, 16)


def test_container_inspect_does_not_persist_environment_secrets():
    class Result:
        returncode = 0
        stdout = json.dumps([{
            "Id": "container-id", "Name": "/fixture", "Config": {
                "Image": "pgvector/pgvector:pg16", "Env": ["POSTGRES_PASSWORD=secret"]},
            "State": {"Status": "running", "Running": True, "OOMKilled": False},
            "HostConfig": {"ShmSize": 536870912, "Memory": 0, "NanoCpus": 0},
            "Mounts": [{"Type": "volume", "Name": "volume-id", "Destination": "/data", "RW": True}],
        }])

    captured = recovery._safe_container_inspect(Result())

    serialized = json.dumps(captured)
    assert "secret" not in serialized
    assert "POSTGRES_PASSWORD" not in serialized
    assert captured["value"]["limits"]["shm_size_bytes"] == 536870912
