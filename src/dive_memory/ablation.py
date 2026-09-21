from __future__ import annotations

from time import perf_counter
from typing import Any, Sequence

from .eval_metrics import aggregate_ranking_rows, classification_metrics
from .evaluation import EvalCase
from .retrieval import ablation_configs
from .service import MemoryService


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def value(percent: float) -> float:
        if not ordered:
            return 0.0
        return ordered[round((len(ordered) - 1) * percent)]

    return {"p50": value(0.50), "p95": value(0.95), "p99": value(0.99)}


def run_ablation(service: MemoryService, cases: Sequence[EvalCase]) -> dict[str, dict[str, Any]]:
    """Run deterministic A-H retrieval variants against one fixed case list."""

    output: dict[str, dict[str, Any]] = {}
    for name, config in ablation_configs().items():
        ranking_rows: list[dict[str, Any]] = []
        gold_abstention: list[str] = []
        predicted_abstention: list[str] = []
        latencies: list[float] = []
        temporal_hits: list[bool] = []
        for case in cases:
            started = perf_counter()
            result = service.retrieve(case.namespace, case.question, config=config)
            latencies.append((perf_counter() - started) * 1000)
            returned = [item.memory.id for item in result.items]
            grades = case.relevance_grades or {memory_id: 1.0 for memory_id in case.expected_memory_ids}
            ranking_rows.append({
                "retrieved": returned,
                "relevance_grades": grades,
                "query_type": case.query_type,
            })
            gold_abstention.append("abstain" if case.should_abstain else "answer")
            predicted_abstention.append("abstain" if not returned else "answer")
            if case.query_type in {"temporal", "correction", "knowledge-update"} and grades:
                temporal_hits.append(bool(set(returned) & set(grades)))

        ranking = aggregate_ranking_rows(ranking_rows, ks=(5, 10))
        abstention = classification_metrics(gold_abstention, predicted_abstention, ["abstain", "answer"])
        output[name] = {
            "configuration": {
                "name": config.name,
                "version": config.version,
                "channels": sorted(config.enabled_channels),
                "rrf_k": config.rrf_k,
                "weights": dict(config.channel_weights),
                "depths": dict(config.candidate_depths),
            },
            "recall_at_5": ranking["recall_at_k"]["5"],
            "precision_at_5": ranking["precision_at_k"]["5"],
            "mrr": ranking["mrr"],
            "ndcg_at_10": ranking["ndcg_at_k"]["10"],
            "temporal_accuracy": sum(temporal_hits) / len(temporal_hits) if temporal_hits else 0.0,
            "abstention_f1": abstention["per_label"]["abstain"]["f1"],
            "latency_ms": _percentiles(latencies),
            "cost": {"api_calls": 0, "usd": 0.0, "note": "provider cost not reported by local core"},
            "slices": ranking["slices"],
        }
    return output
