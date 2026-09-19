from __future__ import annotations

from dataclasses import dataclass
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
    rows: list[dict[str, Any]] = []
    for case in cases:
        result = service.retrieve(case.namespace, case.question)
        returned = [item.memory.id for item in result.items]
        hit = bool(set(returned) & set(case.expected_memory_ids))
        abstain = not result.items
        hits += int(hit)
        abstention_correct += int(abstain == case.should_abstain)
        if not case.should_abstain and result.items and not hit and case.expected_memory_ids:
            false_memory += 1
        rows.append({"case_id": case.case_id, "hit": hit, "abstain": abstain, "returned": returned})
    total = len(cases) or 1
    return {"cases": len(cases), "recall": hits / total, "abstention_accuracy": abstention_correct / total,
            "false_memory_rate": false_memory / total, "rows": rows}
