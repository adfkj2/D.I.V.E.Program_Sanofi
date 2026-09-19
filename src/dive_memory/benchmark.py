from __future__ import annotations

import argparse
import json
from statistics import median
from time import perf_counter

from .evaluation import EvalCase, run_cases
from .service import MemoryService


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[round((len(ordered) - 1) * fraction)]


def run_load_benchmark(memory_count: int = 10_000, query_iterations: int = 50) -> dict:
    """Run the reproducible local scale smoke used before PostgreSQL tests.

    This measures the SQLite development adapter and deliberately labels it as
    such. The production PostgreSQL p95 gate must be run separately against a
    deployed pgvector instance.
    """
    memory_count = max(1, int(memory_count))
    query_iterations = max(1, int(query_iterations))
    service = MemoryService()
    ingest_ms: list[float] = []
    memory_ids: list[str] = []
    for index in range(memory_count):
        started = perf_counter()
        result = service.ingest(
            "benchmark-user", f"请记住项目事实 project-{index}", explicit=True,
            idempotency_key=f"benchmark-{index}",
        )
        ingest_ms.append((perf_counter() - started) * 1000)
        memory_ids.extend(result["memory_ids"])

    query_ms: list[float] = []
    iterations = query_iterations
    for index in range(iterations):
        target = (index * max(1, memory_count // iterations)) % memory_count
        started = perf_counter()
        service.retrieve("benchmark-user", f"project-{target}", limit=5, token_budget=1500)
        query_ms.append((perf_counter() - started) * 1000)

    target = memory_count - 1
    evaluation = run_cases(service, [
        EvalCase("known", "benchmark-user", f"project-{target}", str(target), [memory_ids[target]]),
        EvalCase("unknown", "benchmark-user", "不存在的火星项目", None, [], should_abstain=True),
    ])
    return {
        "storage": "sqlite-development",
        "memory_count": memory_count,
        "query_iterations": iterations,
        "ingest_ms": {"p50": median(ingest_ms), "p95": _percentile(ingest_ms, 0.95),
                      "p99": _percentile(ingest_ms, 0.99)},
        "retrieve_ms": {"p50": median(query_ms), "p95": _percentile(query_ms, 0.95),
                        "p99": _percentile(query_ms, 0.99)},
        "quality": {key: evaluation[key] for key in (
            "recall", "abstention_accuracy", "false_memory_rate", "provenance_coverage"
        )},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the D.I.V.E. local memory benchmark")
    parser.add_argument("--memories", type=int, default=10_000)
    parser.add_argument("--queries", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(run_load_benchmark(args.memories, args.queries), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
