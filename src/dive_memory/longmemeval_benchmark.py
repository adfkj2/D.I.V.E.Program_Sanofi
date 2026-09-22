from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from math import log2
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Iterable, Sequence

from .answering import AnswerStatus, GroundedReader, decide_grounded_answer
from .eval_adapters.longmemeval import LongMemEvalAdapter, LongMemEvalCase
from .eval_manifest import build_run_manifest
from .extraction import assertion_segments
from .retrieval import RetrievalConfig
from .service import MemoryService


_WARM_BATCH = 32  # measured sweet spot on CPU bge-m3; see ops/gate_batch_probe.py


def _gate_device(gate: Any) -> str | None:
    """Report the encoder's real torch device so a run is auditable.

    GPU and CPU runs of the same configuration are not interchangeable: the
    measured per-case wall clock differs by more than an order of magnitude
    (see ``docs/benchmark/longmemeval-500case-profiling.md``). Recording the
    device in the artifact prevents a fast GPU run from being compared against
    a slow CPU run without that difference being visible.
    """
    encoder = getattr(getattr(gate, "index", None), "encoder", None)
    if encoder is None:
        return None
    device = getattr(encoder, "device", None)
    if device is None:
        return None
    return str(device)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def _latency(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "p50": median(values) if values else 0.0,
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0


def _unit_ndcg(units_by_rank: Sequence[Sequence[str]], ideal_relevant: int, k: int) -> float:
    seen: set[str] = set()
    dcg = 0.0
    for rank, units in enumerate(units_by_rank[:k], start=1):
        new_units = set(units) - seen
        if new_units:
            dcg += 1.0 / log2(rank + 1.0)
            seen.update(new_units)
    idcg = sum(1.0 / log2(rank + 1.0) for rank in range(1, min(ideal_relevant, k) + 1))
    return dcg / idcg if idcg else 0.0


def _ranking_summary(rows: Sequence[dict[str, Any]], *, unit: str) -> dict[str, Any]:
    answerable = [row for row in rows if not row["should_abstain"]]
    output: dict[str, Any] = {"cases": len(answerable)}
    for k in (1, 5, 10):
        output[f"recall_any_at_{k}"] = _mean(
            float(any(row[f"{unit}_relevant_by_rank"][:k])) for row in answerable
        )
        output[f"recall_all_at_{k}"] = _mean(
            float(row[f"{unit}_coverage_at_{k}"] == 1.0) for row in answerable
        )
        output[f"evidence_coverage_at_{k}"] = _mean(
            float(row[f"{unit}_coverage_at_{k}"]) for row in answerable
        )
        output[f"precision_at_{k}"] = _mean(
            sum(row[f"{unit}_relevant_by_rank"][:k]) / k for row in answerable
        )
        output[f"ndcg_at_{k}"] = _mean(
            _unit_ndcg(
                row[f"{unit}_relevant_units_by_rank"],
                row[f"{unit}_gold_count"],
                k,
            )
            for row in answerable
        )
    output["mrr"] = _mean(
        next((1.0 / rank for rank, hit in enumerate(row[f"{unit}_relevant_by_rank"], start=1) if hit), 0.0)
        for row in answerable
    )
    return output


def _role_source_type(turn_role: str) -> str:
    """Map a chat role onto the v2 provenance vocabulary.

    The v2 gate trusts first-party user statements and treats ``assistant`` /
    ``system`` turns as conditional. Deciding who spoke is *not* deciding
    whether the text is worth remembering; the gate still does the latter.
    """
    return "user" if str(turn_role).casefold() in {"user", "human"} else "assistant"


def _evaluate_case(case: LongMemEvalCase, *, limit: int,
                   gate: Any | None = None,
                   reader: GroundedReader | None = None) -> tuple[dict[str, Any], dict[str, float | int]]:
    service = MemoryService(gate=gate)
    turn_to_event: dict[str, str] = {}
    event_to_session: dict[str, str] = {}
    session_to_events: dict[str, set[str]] = defaultdict(set)
    evidence_memories: set[str] = set()
    formed_memories: set[str] = set()
    nonempty_turns = 0
    # Warm one bounded turn block immediately before ingesting that same block.
    # This keeps a small cache hot, includes assertion spans used by the mixed
    # turn fallback, and prevents a whole-case warm-up from evicting its own
    # earliest vectors before ingestion begins.
    warm = getattr(gate, "warm", None)
    requires_encoding = getattr(gate, "requires_encoding", None)
    nonempty = [turn for turn in case.turns if turn.content.strip()]
    ingest_started = perf_counter()
    try:
        for start in range(0, len(nonempty), _WARM_BATCH):
            turn_batch = nonempty[start:start + _WARM_BATCH]
            if callable(warm):
                pending: list[str] = []
                for turn in turn_batch:
                    source_type = _role_source_type(turn.role)
                    values = [turn.content, *assertion_segments(turn.content)]
                    pending.extend(
                        value for value in values
                        if not callable(requires_encoding) or requires_encoding(
                            value, explicit=False, source_type=source_type,
                        )
                    )
                warm(pending)
            for turn in turn_batch:
                nonempty_turns += 1
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
                event_id = result["event_id"]
                turn_to_event[turn.ref] = event_id
                event_to_session[event_id] = turn.session_id
                session_to_events[turn.session_id].add(event_id)
                formed_memories.update(result["memory_ids"])
                if turn.has_answer:
                    evidence_memories.update(result["memory_ids"])
        ingest_ms = (perf_counter() - ingest_started) * 1000

        gold_turn_events = {
            turn_to_event[turn.ref]
            for turn in case.turns
            if turn.has_answer and turn.ref in turn_to_event
        }
        gold_session_ids = set(case.evidence_session_ids)

        config = RetrievalConfig.full(name="longmemeval-phase4-default")
        retrieve_started = perf_counter()
        result = service.retrieve(
            case.namespace,
            case.question,
            limit=limit,
            as_of=case.question_date_iso,
            config=config,
        )
        retrieve_ms = (perf_counter() - retrieve_started) * 1000
        answer_decision = (
            decide_grounded_answer(
                reader,
                case.question,
                result.items,
                as_of=case.question_date_iso,
            )
            if reader is not None else None
        )

        returned_sources: list[set[str]] = [set(item.source_refs) for item in result.items]
        turn_relevant_units = [sorted(sources & gold_turn_events) for sources in returned_sources]
        session_relevant_units = [
            sorted({event_to_session[event] for event in sources if event in event_to_session} & gold_session_ids)
            for sources in returned_sources
        ]
        turn_relevant = [bool(units) for units in turn_relevant_units]
        session_relevant = [bool(units) for units in session_relevant_units]

        row: dict[str, Any] = {
            "question_id": case.question_id,
            "question_type": case.question_type,
            "should_abstain": case.should_abstain,
            "nonempty_turns": nonempty_turns,
            "formed_memory_count": len(formed_memories),
            "turn_gold_count": len(gold_turn_events),
            "session_gold_count": len(gold_session_ids),
            "evidence_memory_count": len(evidence_memories),
            "turn_relevant_by_rank": turn_relevant,
            "session_relevant_by_rank": session_relevant,
            "turn_relevant_units_by_rank": turn_relevant_units,
            "session_relevant_units_by_rank": session_relevant_units,
            "returned_memory_ids": [item.memory.id for item in result.items],
            "returned_source_counts": [len(sources) for sources in returned_sources],
            "retrieval_empty": not bool(result.items),
            "predicted_abstain": (
                answer_decision.status is AnswerStatus.ABSTAIN if answer_decision is not None else None
            ),
            "answer_decision": answer_decision.to_dict() if answer_decision is not None else None,
            "ingest_ms": ingest_ms,
            "retrieve_ms": retrieve_ms,
            "executed_channels": result.trace["executed_channels"],
            "degraded": result.degraded,
        }
        for k in (1, 5, 10):
            top_sources = set().union(*returned_sources[:k]) if returned_sources[:k] else set()
            row[f"turn_coverage_at_{k}"] = (
                len(top_sources & gold_turn_events) / len(gold_turn_events) if gold_turn_events else 0.0
            )
            returned_sessions = {
                event_to_session[event] for event in top_sources if event in event_to_session
            }
            row[f"session_coverage_at_{k}"] = (
                len(returned_sessions & gold_session_ids) / len(gold_session_ids) if gold_session_ids else 0.0
            )
        return row, {
            "nonempty_turns": nonempty_turns,
            "formed_memories": len(formed_memories),
            "gold_evidence_turns": len(gold_turn_events),
            "formed_evidence_turns": sum(
                bool(service.store.memories_for_event(event_id)) for event_id in gold_turn_events
            ),
        }
    finally:
        service.store.close()


def run_longmemeval_retrieval(
    dataset_path: str | Path,
    *,
    max_cases: int | None = None,
    limit: int = 10,
    repository_root: str | Path = ".",
    gate: Any | None = None,
    gate_name: str = "utility-baseline-v1",
    reader: GroundedReader | None = None,
    reader_name: str | None = None,
) -> dict[str, Any]:
    cases = LongMemEvalAdapter.load(dataset_path)
    if max_cases is not None:
        cases = cases[:max(0, int(max_cases))]
    started = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    totals: dict[str, int] = defaultdict(int)
    for case in cases:
        try:
            row, counts = _evaluate_case(case, limit=limit, gate=gate, reader=reader)
            rows.append(row)
            for key, value in counts.items():
                totals[key] += int(value)
        except Exception as exc:  # keep per-case failure evidence
            errors.append({
                "question_id": case.question_id,
                "error_type": type(exc).__name__,
                "message": str(exc),
            })

    completed = datetime.now(timezone.utc)
    answerable = [row for row in rows if not row["should_abstain"]]
    abstention = [row for row in rows if row["should_abstain"]]
    reader_name = reader_name or (
        str(getattr(reader, "policy_version", type(reader).__name__)) if reader is not None else None
    )
    configuration = {
        "adapter": "longmemeval-adapter-v2",
        "formation": "core heuristic provider; explicit=false; empty turns preserved by adapter and skipped by runner",
        "write_gate": gate_name,
        "write_gate_runtime": {
            "max_sequence_length": getattr(gate, "max_sequence_length", None),
            "encode_batch_size": getattr(gate, "encode_batch_size", None),
            "cache_size": getattr(getattr(gate, "index", None), "cache_size", None),
            "device": _gate_device(gate),
        },
        "source_type_policy": "user turns -> 'user'; assistant/system turns -> 'assistant'",
        "retrieval": "full channels except multi-hop; deterministic vectors excluded from semantic dense scoring",
        "limit": limit,
        "max_cases": max_cases,
        "gold_visibility": "question/answer/type/evidence labels evaluation-only; writer sees role/content/session/time",
        "reader": reader_name or "NOT_CONFIGURED",
        "qa_stage": (
            "NON_OFFICIAL_READER_CONFIGURED" if reader is not None
            else "NOT_COMPLETED: no configured reader model credentials"
        ),
    }
    manifest = build_run_manifest(
        run_id=f"longmemeval-retrieval-{started.strftime('%Y%m%dT%H%M%SZ')}",
        dataset_name="LongMemEval_S-cleaned",
        dataset_version="hf-98d7416c24c778c2fee6e6f3006e7a073259d48f",
        dataset_path=dataset_path,
        configuration=configuration,
        repository_root=repository_root,
    ).to_dict()
    report: dict[str, Any] = {
        "schema_version": "dive-longmemeval-retrieval-v2",
        "status": "PARTIAL",
        "started_at": started.isoformat(),
        "finished_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "manifest": manifest,
        "official_scope": {
            "dataset_cases": len(cases),
            "retrieval_stage": "COMPLETED" if len(rows) == len(cases) else "PARTIAL",
            "reader_stage": "COMPLETED_NON_OFFICIAL" if reader is not None else "NOT_EVALUATED",
            "qa_generation": "COMPLETED_NON_OFFICIAL" if reader is not None else "NOT_COMPLETED",
            "official_qa_judge": "NOT_COMPLETED",
            "reason": (
                "A non-official grounded reader was evaluated; the official judge was not invoked."
                if reader is not None else
                "No configured reader/judge model credentials; these are retrieval-stage results only."
            ),
        },
        "formation": {
            **totals,
            "memory_per_nonempty_turn": (
                totals["formed_memories"] / totals["nonempty_turns"] if totals["nonempty_turns"] else 0.0
            ),
            "gold_evidence_turn_formation_coverage": (
                totals["formed_evidence_turns"] / totals["gold_evidence_turns"]
                if totals["gold_evidence_turns"] else 0.0
            ),
        },
        "retrieval": {
            "turn_level": _ranking_summary(rows, unit="turn"),
            "session_level": _ranking_summary(rows, unit="session"),
            "latency_ms": _latency([float(row["retrieve_ms"]) for row in rows]),
            "answerable_cases": len(answerable),
        },
        "abstention": _abstention_summary(rows, reader_configured=reader is not None),
        "ingest": {
            "case_latency_ms": _latency([float(row["ingest_ms"]) for row in rows]),
        },
        "errors": errors,
        "timeouts": [],
        "rows": rows,
        "limitations": [
            (
                "A non-official reader ran, but the official GPT-4o judge did not; no official overall QA score is claimed."
                if reader is not None else
                "No reader generation or official GPT-4o judge was run; semantic abstention is NOT_EVALUATED."
            ),
            "The core heuristic extractor and non-semantic deterministic embedding are retained as-is.",
            "Memory-level rankings differ from the official raw turn/session retrieval baselines.",
            "Official same-calendar-day randomized clock times are treated as day-precision cutoff data.",
            "Thirteen cases contain duplicate filler session ids and twelve turns are empty in the official release.",
        ],
    }
    type_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        type_rows[row["question_type"]].append(row)
    report["retrieval"]["by_question_type"] = {
        question_type: {
            "turn_level": _ranking_summary(group, unit="turn"),
            "session_level": _ranking_summary(group, unit="session"),
        }
        for question_type, group in sorted(type_rows.items())
    }
    return report


def _abstention_summary(rows: Sequence[dict[str, Any]], *, reader_configured: bool) -> dict[str, Any]:
    negatives = [row for row in rows if row["should_abstain"]]
    answerable = [row for row in rows if not row["should_abstain"]]
    retrieval_empty = sum(bool(row["retrieval_empty"]) for row in rows)
    base: dict[str, Any] = {
        "status": "EVALUATED" if reader_configured else "NOT_EVALUATED",
        "cases": len(negatives),
        "retrieval_empty_cases": retrieval_empty,
        "retrieval_empty_rate": retrieval_empty / len(rows) if rows else 0.0,
    }
    if not reader_configured:
        return {
            **base,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "false_abstention_rate": None,
            "answer_coverage": None,
            "error_rate": None,
            "note": "Candidate absence is not semantic abstention; configure a grounded reader to evaluate it.",
        }

    evaluated = [row for row in rows if row["answer_decision"]["status"] != AnswerStatus.ERROR.value]
    errors = len(rows) - len(evaluated)
    true_positive = sum(
        row["should_abstain"] and row["predicted_abstain"] is True for row in evaluated
    )
    false_positive = sum(
        not row["should_abstain"] and row["predicted_abstain"] is True for row in evaluated
    )
    false_negative = sum(
        row["should_abstain"] and row["predicted_abstain"] is False for row in evaluated
    )
    true_negative = sum(
        not row["should_abstain"] and row["predicted_abstain"] is False for row in evaluated
    )
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {
        **base,
        "accuracy": recall,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "false_abstention_rate": false_positive / len(answerable) if answerable else 0.0,
        "answer_coverage": (true_negative + false_negative) / len(rows) if rows else 0.0,
        "error_rate": errors / len(rows) if rows else 0.0,
        "confusion": {
            "true_abstain": true_positive,
            "false_abstain": false_positive,
            "missed_abstain": false_negative,
            "true_answer": true_negative,
            "errors": errors,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the D.I.V.E retrieval stage on official LongMemEval")
    parser.add_argument("dataset")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--gate", choices=("v1", "semantic"), default="v1",
                        help="v1 keeps the keyword baseline; semantic loads the local bge-m3 v2 gate")
    parser.add_argument("--gate-cache", type=int, default=1024,
                        help="bounded vector cache for the semantic gate (0 disables)")
    parser.add_argument("--gate-batch-size", type=int, default=8,
                        help="safe internal sentence-transformer batch size")
    parser.add_argument("--gate-max-sequence-length", type=int, default=512,
                        help="truncate gate inputs to this many model tokens")
    parser.add_argument("--device", default="cpu",
                        help="torch device for the semantic gate encoder, e.g. 'cpu' or 'cuda'")
    args = parser.parse_args()
    gate: Any | None = None
    gate_name = "utility-baseline-v1"
    if args.gate == "semantic":
        from .semantic_gate import load_local_gate, POLICY_VERSION

        gate = load_local_gate(
            cache_size=args.gate_cache,
            encode_batch_size=max(1, args.gate_batch_size),
            max_sequence_length=max(1, args.gate_max_sequence_length),
            device=None if args.device == "cpu" else args.device,
        )
        gate_name = POLICY_VERSION
    report = run_longmemeval_retrieval(
        args.dataset,
        max_cases=args.max_cases,
        limit=max(1, args.limit),
        repository_root=Path.cwd(),
        gate=gate,
        gate_name=gate_name,
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
