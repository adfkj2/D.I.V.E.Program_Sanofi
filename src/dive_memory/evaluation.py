from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .service import MemoryService


@dataclass(slots=True)
class EvalCase:
    case_id: str
    namespace: str
    question: str
    answer: str | None
    expected_memory_ids: list[str]
    should_abstain: bool = False


def run_cases(service: MemoryService, cases: list[EvalCase]) -> dict[str, Any]:
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
        rows.append({"case_id": case.case_id, "hit": hit, "abstain": abstain,
                     "returned": returned, "latency_ms": latency_ms})
    total = len(cases) or 1
    ordered = sorted(latencies_ms)

    def percentile(percent: float) -> float:
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percent)))
        return ordered[index]

    return {
        "cases": len(cases),
        "answerable_cases": answerable,
        "recall": hits / (answerable or 1),
        "abstention_accuracy": abstention_correct / total,
        "false_memory_rate": false_memory / total,
        "provenance_coverage": provenance_supported / (returned_with_sources or 1),
        "latency_ms": {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)},
        "rows": rows,
    }
