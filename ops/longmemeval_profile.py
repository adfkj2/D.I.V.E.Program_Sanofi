"""Profile the LongMemEval per-case pipeline by stage.

The brief asks for bottleneck profiling rather than guessing. This harness
re-implements the `_evaluate_case` stage boundaries with timers around each
part so the cost split is measured, not assumed. It uses the same
MemoryService / RetrievalConfig / adapter as the real runner, so the numbers
are directly comparable.

Usage:
    python ops/longmemeval_profile.py <dataset> --cases 5 --gate v1
    python ops/longmemeval_profile.py <dataset> --cases 5 --gate semantic
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dive_memory.eval_adapters.longmemeval import LongMemEvalAdapter  # noqa: E402
from dive_memory.extraction import assertion_segments  # noqa: E402
from dive_memory.retrieval import RetrievalConfig  # noqa: E402
from dive_memory.service import MemoryService  # noqa: E402

_WARM_BATCH = 32


def _role_source_type(role: str) -> str:
    return "user" if str(role).casefold() in {"user", "human"} else "assistant"


def profile_case(case, *, limit: int, gate) -> dict[str, float | int]:
    timings: dict[str, float] = defaultdict(float)
    service = MemoryService(gate=gate)
    try:
        nonempty = [t for t in case.turns if t.content.strip()]
        warm = getattr(gate, "warm", None)
        requires_encoding = getattr(gate, "requires_encoding", None)

        # ---- segmentation (pure Python, no encoder) ----
        t0 = perf_counter()
        segments = {t.ref: assertion_segments(t.content) for t in nonempty}
        timings["segment_ms"] += (perf_counter() - t0) * 1000

        # ---- gate warm-up (encoder pre-pass) ----
        if callable(warm):
            t0 = perf_counter()
            for start in range(0, len(nonempty), _WARM_BATCH):
                batch = nonempty[start:start + _WARM_BATCH]
                pending: list[str] = []
                for turn in batch:
                    source_type = _role_source_type(turn.role)
                    values = [turn.content, *segments[turn.ref]]
                    pending.extend(
                        v for v in values
                        if not callable(requires_encoding)
                        or requires_encoding(v, explicit=False, source_type=source_type)
                    )
                warm(pending)
            timings["warm_ms"] += (perf_counter() - t0) * 1000

        # ---- ingest (extraction + gate + embed + projections + sqlite) ----
        t0 = perf_counter()
        formed: set[str] = set()
        for turn in nonempty:
            result = service.ingest(
                case.namespace,
                turn.content,
                event_type=f"longmemeval_{turn.role.casefold()}",
                explicit=False,
                observed_at=turn.occurred_at_iso,
                idempotency_key=f"longmemeval:{case.question_id}:{turn.ref}",
                source_message_id=turn.ref,
                source_type=_role_source_type(turn.role),
            )
            formed.update(result["memory_ids"])
        timings["ingest_ms"] += (perf_counter() - t0) * 1000

        # ---- retrieve ----
        config = RetrievalConfig.full(name="longmemeval-phase4-default")
        t0 = perf_counter()
        result = service.retrieve(
            case.namespace, case.question, limit=limit,
            as_of=case.question_date_iso, config=config,
        )
        timings["retrieve_ms"] += (perf_counter() - t0) * 1000

        timings["nonempty_turns"] = len(nonempty)
        timings["formed_memories"] = len(formed)
        timings["returned_items"] = len(result.items)
        return dict(timings)
    finally:
        service.store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument("--cases", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--gate", choices=("v1", "semantic"), default="v1")
    parser.add_argument("--output")
    args = parser.parse_args()

    gate = None
    gate_name = "utility-baseline-v1"
    if args.gate == "semantic":
        from dive_memory.semantic_gate import load_local_gate, POLICY_VERSION

        gate = load_local_gate(cache_size=1024, encode_batch_size=8, max_sequence_length=512)
        gate_name = POLICY_VERSION

    cases = LongMemEvalAdapter.load(args.dataset)[:args.cases]
    per_case = []
    totals: dict[str, float] = defaultdict(float)
    wall_start = perf_counter()
    for case in cases:
        t0 = perf_counter()
        row = profile_case(case, limit=args.limit, gate=gate)
        row["case_id"] = case.question_id
        row["total_ms"] = (perf_counter() - t0) * 1000
        per_case.append(row)
        for key, value in row.items():
            if key.endswith("_ms") or key == "total_ms":
                totals[key] += float(value)
    wall = perf_counter() - wall_start

    n = len(per_case)
    summary = {
        "gate": gate_name,
        "cases": n,
        "dataset": str(args.dataset),
        "wall_clock_seconds": round(wall, 3),
        "per_case_mean_ms": {
            key: round(value / n, 2) for key, value in sorted(totals.items()) if n
        },
        "per_case_total_ms": {key: round(value, 2) for key, value in sorted(totals.items())},
        "throughput_cases_per_second": round(n / wall, 4) if wall else 0.0,
        "mean_nonempty_turns": round(sum(r["nonempty_turns"] for r in per_case) / n, 1) if n else 0,
        "mean_formed_memories": round(sum(r["formed_memories"] for r in per_case) / n, 1) if n else 0,
        "rows": per_case,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
