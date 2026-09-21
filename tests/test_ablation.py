from dive_memory.ablation import run_ablation
from dive_memory.evaluation import EvalCase
from dive_memory.service import MemoryService


def test_a_h_ablation_outputs_required_quality_and_latency_columns():
    service = MemoryService()
    written = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    cases = [
        EvalCase("known", "u1", "我喜欢什么", None, written["memory_ids"], query_type="preference"),
        EvalCase("negative", "u1", "火星有什么", None, [], should_abstain=True, query_type="negative"),
    ]

    report = run_ablation(service, cases)

    assert list(report) == list("ABCDEFGH")
    for row in report.values():
        assert set(row) >= {
            "configuration", "recall_at_5", "precision_at_5", "mrr", "ndcg_at_10",
            "temporal_accuracy", "abstention_f1", "latency_ms", "cost",
        }
        assert set(row["latency_ms"]) == {"p50", "p95", "p99"}
