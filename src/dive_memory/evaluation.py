from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .eval_metrics import aggregate_ranking_rows, classification_metrics
from .service import MemoryService


@dataclass(slots=True)
class EvalCase:
    case_id: str
    namespace: str
    question: str
    answer: str | None
    expected_memory_ids: list[str]
    should_abstain: bool = False
    query_type: str = "factual"
    relevance_grades: dict[str, float] | None = None


def run_cases(
    service: MemoryService,
    cases: list[EvalCase],
    *,
    ks: tuple[int, ...] = (1, 5, 10),
) -> dict[str, Any]:
    hits = 0
    abstention_correct = 0
    false_memory = 0
    answerable = 0
    provenance_supported = 0
    returned_with_sources = 0
    latencies_ms: list[float] = []
    rows: list[dict[str, Any]] = []
    for case in cases:
        started = perf_counter()
        result = service.retrieve(case.namespace, case.question)
        latency_ms = (perf_counter() - started) * 1000
        latencies_ms.append(latency_ms)
        returned = [item.memory.id for item in result.items]
        hit = bool(set(returned) & set(case.expected_memory_ids))
        abstain = not result.items
        if not case.should_abstain and case.expected_memory_ids:
            answerable += 1
        hits += int(hit)
        abstention_correct += int(abstain == case.should_abstain)
        # Both unsupported answers and answers to a gold-abstain question are
        # false memories. The old condition accidentally ignored the latter.
        if (case.should_abstain and result.items) or (
            not case.should_abstain and result.items and not hit and case.expected_memory_ids
        ):
            false_memory += 1
        for item in result.items:
            returned_with_sources += 1
            provenance_supported += int(bool(item.source_refs))
        grades = case.relevance_grades or {item_id: 1.0 for item_id in case.expected_memory_ids}
        rows.append({"case_id": case.case_id, "hit": hit, "abstain": abstain,
                     "returned": returned, "retrieved": returned,
                     "relevance_grades": grades, "query_type": case.query_type,
                     "should_abstain": case.should_abstain, "latency_ms": latency_ms})
    total = len(cases) or 1
    ordered = sorted(latencies_ms)

    def percentile(percent: float) -> float:
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percent)))
        return ordered[index]

    ranking = aggregate_ranking_rows(rows, ks=ks)
    abstention = classification_metrics(
        ["abstain" if case.should_abstain else "answer" for case in cases],
        ["abstain" if row["abstain"] else "answer" for row in rows],
        ["abstain", "answer"],
    )
    abstain_label = abstention["per_label"]["abstain"]
    slices: dict[str, dict[str, Any]] = {}
    for query_type in sorted({case.query_type for case in cases}):
        indices = [index for index, case in enumerate(cases) if case.query_type == query_type]
        slice_rows = [rows[index] for index in indices]
        slice_ranking = ranking["slices"][query_type]
        slices[query_type] = {
            **slice_ranking,
            "abstention_accuracy": (
                sum(slice_rows[offset]["abstain"] == cases[index].should_abstain
                    for offset, index in enumerate(indices)) / len(indices)
                if indices else 0.0
            ),
        }

    return {
        "cases": len(cases),
        "answerable_cases": answerable,
        "recall": hits / (answerable or 1),
        "abstention_accuracy": abstention_correct / total,
        "false_memory_rate": false_memory / total,
        "provenance_coverage": provenance_supported / (returned_with_sources or 1),
        "retrieval": {key: value for key, value in ranking.items() if key != "slices"},
        "abstention": {
            "accuracy": abstention["accuracy"],
            "precision": abstain_label["precision"],
            "recall": abstain_label["recall"],
            "f1": abstain_label["f1"],
            "confusion": abstention,
        },
        "slices": slices,
        "latency_ms": {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)},
        "rows": rows,
    }
