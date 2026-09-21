import json

import pytest

from dive_memory import postgres_scale_benchmark as scale


def _context(tmp_path):
    run_id = "postgres-100k-20260921-000000"
    manifest = scale._new_manifest(run_id, scale._schema_name(run_id), {
        "memory_count": scale.DATASET_SIZE,
        "seed": scale.SEED,
        "embedding_dimensions": 96,
    })
    ctx = scale.RunContext(run_id, tmp_path / run_id, "redacted", "fixture-container", manifest)
    ctx.run_dir.mkdir(parents=True)
    scale._save_manifest(ctx)
    return ctx


def test_atomic_json_writer_round_trips_and_hashes(tmp_path):
    path = tmp_path / "artifact.json"
    digest = scale._write_json(path, {"status": "COMPLETED", "value": "中文"})

    assert json.loads(path.read_text(encoding="utf-8"))["value"] == "中文"
    assert digest == scale._file_sha256(path)
    assert not path.with_suffix(".json.tmp").exists()


def test_manifest_binds_run_schema_config_and_checkpoints(tmp_path):
    ctx = _context(tmp_path)
    loaded = scale._read_json(ctx.run_dir / "manifest.json")

    assert loaded["run_id"] == ctx.run_id
    assert loaded["schema"] == scale._schema_name(ctx.run_id)
    assert loaded["config_sha256"] == scale._sha256_bytes(scale._canonical_bytes(loaded["config"]))
    assert set(loaded["checkpoints"]) == set("ABCDEFGH")
    assert all(value["status"] == "NOT_COMPLETED" for value in loaded["checkpoints"].values())
    assert loaded["safety"]["drop_schema_on_failure"] is False
    assert loaded["safety"]["ann_indexes_allowed"] is False


def test_summary_includes_requested_latency_statistics():
    result = scale._summary([1.0, 2.0, 3.0, 4.0, 100.0])

    assert result["count"] == 5
    assert result["p50"] == 3.0
    assert result["p95"] >= result["p50"]
    assert result["p99"] >= result["p95"]
    assert result["mean"] == 22.0
    assert result["min"] == 1.0
    assert result["max"] == 100.0


def test_checkpoint_publishes_then_skips_valid_pass(monkeypatch, tmp_path):
    ctx = _context(tmp_path)
    monkeypatch.setattr(scale, "_ensure_main_count", lambda _ctx: {"memories": 100000, "vectors": 100000})
    calls = []

    first = scale._checkpoint(ctx, "A", lambda _ctx: calls.append("run") or {"evidence": 1})
    second = scale._checkpoint(ctx, "A", lambda _ctx: calls.append("rerun") or {"evidence": 2})

    assert first["status"] == "COMPLETED"
    assert second == first
    assert calls == ["run"]
    assert ctx.manifest["checkpoints"]["A"]["status"] == "PASS"
    assert ctx.manifest["last_successful_checkpoint"] == "A"


def test_checkpoint_failure_is_saved_without_advancing(monkeypatch, tmp_path):
    ctx = _context(tmp_path)
    monkeypatch.setattr(scale, "_ensure_main_count", lambda _ctx: {"memories": 100000, "vectors": 100000})

    def fail(_ctx):
        raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError, match="fixture failure"):
        scale._checkpoint(ctx, "A", fail)

    artifact = scale._read_json(ctx.run_dir / "bulk-load.json")
    assert artifact["status"] == "FAILED"
    assert artifact["last_successful_checkpoint"] is None
    assert ctx.manifest["checkpoints"]["A"]["status"] == "FAILED"
    assert ctx.manifest["last_successful_checkpoint"] is None


def test_ann_plan_is_plan_only_and_covers_required_comparison():
    plan = scale._ann_plan()

    assert "PLAN ONLY" in plan
    assert "Exact pgvector cosine" in plan
    assert "HNSW" in plan
    assert "IVFFlat" in plan
    assert "Recall@K" in plan
    assert "do not execute automatically" in plan
