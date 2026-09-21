import pytest

from dive_memory.eval_metrics import aggregate_ranking_rows


def test_aggregate_ranking_rows_uses_macro_averages_and_query_slices():
    rows = [
        {
            "retrieved": ["a", "x"],
            "relevance_grades": {"a": 1.0},
            "query_type": "factual",
        },
        {
            "retrieved": ["x", "b"],
            "relevance_grades": {"b": 1.0},
            "query_type": "temporal",
        },
    ]

    report = aggregate_ranking_rows(rows, ks=(1, 2))

    assert report["precision_at_k"]["1"] == 0.5
    assert report["recall_at_k"]["1"] == 0.5
    assert report["recall_at_k"]["2"] == 1.0
    assert report["mrr"] == 0.75
    assert report["slices"]["factual"]["mrr"] == 1.0
    assert report["slices"]["temporal"]["mrr"] == 0.5


def test_aggregate_ranking_rows_excludes_goldless_abstention_from_retrieval_metrics():
    report = aggregate_ranking_rows([
        {"retrieved": [], "relevance_grades": {}, "query_type": "negative"},
    ], ks=(1, 5))

    assert report["evaluable_cases"] == 0
    assert report["precision_at_k"] == {"1": 0.0, "5": 0.0}
    assert report["slices"]["negative"]["evaluable_cases"] == 0


def test_aggregate_rejects_invalid_rows_and_ks():
    with pytest.raises(ValueError):
        aggregate_ranking_rows([], ks=())
    with pytest.raises(ValueError):
        aggregate_ranking_rows([], ks=(0,))
