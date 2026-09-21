import json

import pytest

from dive_memory.embedding_benchmark import load_dataset, run_embedding_comparison


def test_embedding_dataset_covers_required_memory_slices():
    dataset = load_dataset("eval/datasets/embedding_v1/memory_retrieval.json")
    assert len(dataset["documents"]) == 48
    assert len(dataset["queries"]) == 32
    assert {row["category"] for row in dataset["queries"]} == {
        "semantic_paraphrase", "preference_retrieval", "old_episodic_recall",
        "temporal_change", "conflicting_facts", "near_duplicate_memory",
        "entity_ambiguity", "long_tail_facts",
    }


def test_embedding_dataset_rejects_unknown_gold(tmp_path):
    dataset = load_dataset("eval/datasets/embedding_v1/memory_retrieval.json")
    dataset["queries"][0]["relevant_ids"] = ["missing"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    with pytest.raises(ValueError, match="known relevant_ids"):
        load_dataset(path)


def test_embedding_comparison_runs_dependency_free_baselines(tmp_path):
    report = run_embedding_comparison(
        "eval/datasets/embedding_v1/memory_retrieval.json",
        models=[],
        device="cpu",
        batch_size=2,
        repository_root=tmp_path,
    )
    assert report["status"] == "COMPLETED"
    assert set(report["results"]) == {
        "deterministic-sha256-v1", "bm25-word-cjk-bigram-v1",
    }
    assert report["results"]["deterministic-sha256-v1"]["semantic"] is False
    assert report["results"]["bm25-word-cjk-bigram-v1"]["metrics"]["cases"] == 32
