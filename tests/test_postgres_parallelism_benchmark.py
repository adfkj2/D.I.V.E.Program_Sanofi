import inspect
import json

import pytest

from dive_memory import postgres_parallelism_benchmark as paired


def _schedule(run_id="fixture"):
    return paired.generate_query_schedule(
        run_id, dataset_size=100, worker_levels=(1,), trials=(1, 2),
        operations_per_worker=3, warmups_per_worker=1, seed=42,
    )


def _artifact(schedule_hash, targets, *, status="PASS", correctness="PASS", throughput=10.0):
    return {
        "status": status,
        "query_schedule_sha256": schedule_hash,
        "timed_target_memory_ids": targets,
        "correctness": {"status": correctness},
        "metrics": {"throughput_operations_per_second": throughput,
                    "latency_ms": {"p50": 10.0, "p95": 20.0, "p99": 30.0}},
    }


def test_parallel_and_no_parallel_receive_identical_schedule():
    schedule = _schedule()
    p = paired.entries_for_arm(schedule, 1, 1, "parallel")
    np = paired.entries_for_arm(schedule, 1, 1, "no-parallel")
    assert p == np
    assert paired.target_lists(schedule, 1, 1, "parallel") == paired.target_lists(schedule, 1, 1, "no-parallel")
    assert all("arm" not in row and "branch" not in row for row in schedule["entries"])


def test_execution_path_has_no_branch_specific_rng():
    source = inspect.getsource(paired.execute_trial_arm)
    assert "random" not in source
    assert "randrange" not in source
    assert "query-schedule.json" in source


def test_schedule_hash_is_canonical_and_deterministic():
    first = _schedule("same")
    second = _schedule("same")
    reordered = {key: first[key] for key in reversed(first)}
    assert first == second
    assert paired.schedule_sha256(reordered) == first["query_schedule_sha256"]
    assert paired.verify_schedule(first) == first["query_schedule_sha256"]


def test_schedule_mismatch_blocks_execution_and_comparison():
    schedule = _schedule()
    schedule["entries"][0]["target_memory_id"] += 1
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        paired.verify_schedule(schedule)
    valid = _schedule()
    digest = valid["query_schedule_sha256"]
    comparison = paired.compare_pair(_artifact(digest, [1, 2]), _artifact("0" * 64, [1, 2]))
    assert comparison["status"] == "INVALID"


def test_session_settings_are_two_and_zero():
    class Connection:
        def __init__(self): self.calls = []
        def execute(self, statement, params): self.calls.append((statement, params))

    p, np = Connection(), Connection()
    assert paired.apply_session_setting(p, "parallel") == 2
    assert paired.apply_session_setting(np, "no-parallel") == 0
    assert p.calls[0][1] == ("2",)
    assert np.calls[0][1] == ("0",)


def test_query_plan_validation_enforces_both_arms():
    parallel_plan = [{"Plan": {"Node Type": "Gather", "Workers Planned": 2, "Workers Launched": 2,
                                 "Plans": [{"Node Type": "Parallel Seq Scan", "Parallel Aware": True}]}}]
    serial_plan = [{"Plan": {"Node Type": "Seq Scan", "Parallel Aware": False}}]
    planned_not_launched = [{"Plan": {"Node Type": "Gather", "Workers Planned": 2, "Workers Launched": 0}}]
    assert paired.validate_query_plan(parallel_plan, "parallel")["status"] == "PASS"
    assert paired.validate_query_plan(serial_plan, "no-parallel")["status"] == "PASS"
    assert paired.validate_query_plan(parallel_plan, "no-parallel")["status"] == "FAIL"
    assert paired.validate_query_plan(planned_not_launched, "parallel")["status"] == "FAIL"
    assert paired.validate_query_plan(planned_not_launched, "parallel", require_parallel_execution=False)["status"] == "PASS"
    assert paired.validate_query_plan(serial_plan, "parallel", require_parallel_execution=False)["status"] == "PASS"


def test_counterbalanced_order_is_exact():
    order = paired.generate_execution_order("fixture")
    assert [row["order"] for row in order["trials"]] == [
        ["parallel", "no-parallel"], ["no-parallel", "parallel"],
        ["parallel", "no-parallel"], ["no-parallel", "parallel"],
    ]


def test_resume_returns_pass_artifact_without_database_work(tmp_path):
    manifest = paired.create_run(tmp_path, "resume", dataset_size=100, worker_levels=(1,), trials=(1,),
                                 operations_per_worker=1, warmups_per_worker=1)
    schedule_hash = manifest["query_schedule_sha256"]
    artifact = {"status": "PASS", "query_schedule_sha256": schedule_hash, "sentinel": True}
    path = paired.arm_artifact_path(tmp_path, 1, 1, "parallel")
    paired.atomic_write_json(path, artifact)
    manifest["checkpoints"]["1"]["1"]["parallel"].update({"status": "PASS", "attempt_count": 1})
    paired.atomic_write_json(tmp_path / "manifest.json", manifest)

    resumed = paired.execute_trial_arm(tmp_path, "must-not-connect", "none", 1, 1, "parallel")
    assert resumed["sentinel"] is True
    assert paired.read_json(tmp_path / "manifest.json")["checkpoints"]["1"]["1"]["parallel"]["attempt_count"] == 1


def test_atomic_artifact_write_round_trips_and_leaves_no_temp(tmp_path):
    path = tmp_path / "nested" / "artifact.json"
    digest = paired.atomic_write_json(path, {"status": "PASS", "value": "中文"})
    assert json.loads(path.read_text(encoding="utf-8"))["value"] == "中文"
    assert digest == paired.file_sha256(path)
    assert list(path.parent.glob("*.tmp")) == []


def test_correctness_gate_invalidates_pair_even_when_metrics_exist():
    digest = "a" * 64
    result = paired.compare_pair(
        _artifact(digest, [1], correctness="FAIL", throughput=1000),
        _artifact(digest, [1], throughput=1),
    )
    assert result["status"] == "INVALID"


def test_aggregation_excludes_invalid_trials(tmp_path):
    manifest = paired.create_run(tmp_path, "aggregate", dataset_size=100, worker_levels=(1,), trials=(1, 2),
                                 operations_per_worker=1, warmups_per_worker=1)
    digest = manifest["query_schedule_sha256"]
    paired.atomic_write_json(paired.arm_artifact_path(tmp_path, 1, 1, "parallel"), _artifact(digest, [1], throughput=20))
    paired.atomic_write_json(paired.arm_artifact_path(tmp_path, 1, 1, "no-parallel"), _artifact(digest, [1], throughput=10))
    paired.atomic_write_json(paired.arm_artifact_path(tmp_path, 1, 2, "parallel"), _artifact(digest, [2], correctness="FAIL"))
    paired.atomic_write_json(paired.arm_artifact_path(tmp_path, 1, 2, "no-parallel"), _artifact(digest, [2]))

    summary = paired.aggregate_trials(tmp_path)
    assert summary["workers"]["1"]["valid_trial_count"] == 1
    assert summary["workers"]["1"]["excluded_trial_count"] == 1
    assert summary["workers"]["1"]["paired_differences"]["throughput"]["median"] == 10


def test_artifact_validation_checks_schema_hashes_and_secrets(tmp_path):
    paired.create_run(tmp_path, "validate", dataset_size=100, worker_levels=(1,), trials=(1,),
                      operations_per_worker=1, warmups_per_worker=1)
    validation = paired.validate_artifacts(tmp_path)
    assert validation["status"] == "PASS"
    assert validation["secret_scan"] == "PASS"

    order = paired.read_json(tmp_path / "execution-order.json")
    order["trials"][0]["order"].reverse()
    paired.atomic_write_json(tmp_path / "execution-order.json", order)
    assert paired.validate_artifacts(tmp_path)["status"] == "FAIL"
