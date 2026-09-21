import json

import pytest

from dive_memory.evaluation import EvalCase, run_cases
from dive_memory.service import MemoryService
from dive_memory.benchmark import run_load_benchmark
from dive_memory.eval_manifest import build_run_manifest, sha256_file
from dive_memory.eval_metrics import (
    classification_metrics,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_eval_harness_reports_recall_and_abstention():
    service = MemoryService()
    written = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    report = run_cases(service, [
        EvalCase("known", "u1", "绿茶", "绿茶", written["memory_ids"]),
        EvalCase("unknown", "u1", "火星", None, [], should_abstain=True),
    ])
    assert report["cases"] == 2
    assert report["recall"] == 1.0
    assert report["abstention_accuracy"] == 1.0
    assert report["false_memory_rate"] == 0.0
    assert report["provenance_coverage"] == 1.0
    assert report["latency_ms"]["p95"] >= 0


def test_eval_counts_an_answer_to_an_unknown_question_as_false_memory():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    report = run_cases(service, [
        EvalCase("must-abstain", "u1", "我喜欢什么", None, [], should_abstain=True),
    ])
    assert report["false_memory_rate"] == 1.0


def test_local_load_benchmark_reports_quality_and_latency():
    report = run_load_benchmark(memory_count=20, query_iterations=3)
    assert report["storage"] == "sqlite-development"
    assert report["memory_count"] == 20
    assert report["retrieve_ms"]["p95"] >= 0
    assert report["quality"]["false_memory_rate"] == 0.0


def test_local_load_benchmark_clamps_zero_and_negative_sizes():
    report = run_load_benchmark(memory_count=0, query_iterations=-2)
    assert report["memory_count"] == 1
    assert report["query_iterations"] == 1
    assert report["quality"]["recall"] == 1.0


def test_ranking_metrics_match_hand_computed_values():
    retrieved = ["a", "x", "b"]
    relevant = ["a", "b"]

    assert precision_at_k(retrieved, relevant, 1) == 1.0
    assert precision_at_k(retrieved, relevant, 3) == pytest.approx(2 / 3)
    assert recall_at_k(retrieved, relevant, 1) == 0.5
    assert recall_at_k(retrieved, relevant, 3) == 1.0
    assert reciprocal_rank(retrieved, relevant) == 1.0

    # DCG = 7/log2(2) + 1/log2(4) = 7.5; ideal = 7 + 1/log2(3).
    expected = 7.5 / (7.0 + 1.0 / 1.584962500721156)
    assert ndcg_at_k(retrieved, {"a": 3.0, "b": 1.0}, 3) == pytest.approx(expected)


def test_ranking_metrics_handle_duplicates_empty_gold_and_invalid_k():
    assert precision_at_k(["a", "a"], ["a"], 2) == 0.5
    assert recall_at_k(["a", "a"], ["a"], 2) == 1.0
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert ndcg_at_k([], {"a": 1.0}, 10) == 0.0
    assert recall_at_k(["a"], [], 1) == 0.0
    with pytest.raises(ValueError):
        precision_at_k(["a"], ["a"], 0)


def test_classification_metrics_report_per_label_and_macro_scores():
    report = classification_metrics(
        gold=["write", "write", "skip", "review"],
        predicted=["write", "skip", "skip", "write"],
        labels=["write", "skip", "review"],
    )

    assert report["accuracy"] == 0.5
    assert report["per_label"]["write"] == {
        "support": 2,
        "predicted": 2,
        "true_positive": 1,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }
    assert report["per_label"]["review"]["recall"] == 0.0
    assert report["macro_f1"] == pytest.approx((0.5 + 2 / 3 + 0.0) / 3)


def test_eval_harness_reports_real_ranking_and_abstention_metrics():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    second = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    report = run_cases(service, [
        EvalCase(
            "preference", "u1", "我喜欢什么", None,
            [first["memory_ids"][0], second["memory_ids"][0]],
            query_type="preference",
        ),
        EvalCase("unknown", "u1", "火星有什么", None, [], should_abstain=True,
                 query_type="negative"),
    ], ks=(1, 5, 10))

    assert set(report["retrieval"]) >= {"precision_at_k", "recall_at_k", "mrr", "ndcg_at_k"}
    assert report["retrieval"]["recall_at_k"]["5"] == 1.0
    assert report["retrieval"]["mrr"] == 1.0
    assert report["abstention"]["precision"] == 1.0
    assert report["abstention"]["recall"] == 1.0
    assert report["slices"]["preference"]["cases"] == 1
    assert report["slices"]["negative"]["cases"] == 1


def test_run_manifest_is_stable_and_records_dataset_hash(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({"cases": [1, 2]}, sort_keys=True), encoding="utf-8")

    first = build_run_manifest(
        run_id="run-1",
        dataset_name="internal-v1",
        dataset_version="1",
        dataset_path=dataset,
        configuration={"channels": ["bm25"], "top_k": 5},
        repository_root=tmp_path,
    )
    second = build_run_manifest(
        run_id="run-2",
        dataset_name="internal-v1",
        dataset_version="1",
        dataset_path=dataset,
        configuration={"top_k": 5, "channels": ["bm25"]},
        repository_root=tmp_path,
    )

    assert first.dataset_sha256 == sha256_file(dataset)
    assert first.configuration_sha256 == second.configuration_sha256
    assert first.environment["python"]
    assert first.schema_version == "dive-eval-manifest-v1"
