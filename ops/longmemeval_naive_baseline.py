"""Naive dense-vector retrieval baselines for the official LongMemEval retrieval stage.

Why this exists
---------------
`docs/21-phase-4-architecture-review.md` Q3 records that the architecture's
comparative claim -- "hybrid retrieval beats naive vector memory" -- is
**untested**, because no baseline was ever run under the same queries. The
500-case artifact reports absolute recall figures only, and its own `limitations`
say so ("Memory-level rankings differ from the official raw turn/session
retrieval baselines"). Absolute numbers cannot answer a comparative question.

This harness runs the two standard protocol-compatible baselines over the *same*
500 official cases and the *same* metric code, so the only thing that differs is
the retrieval design:

* ``turn``    -- one vector per non-empty turn, ranked by cosine similarity.
                 This is the standard LongMemEval dense retrieval granularity.
* ``session`` -- one vector per session (all of that session's turns joined),
                 ranked by cosine similarity.

Both use the **same local bge-m3 checkpoint, the same sequence-length bound and
the same normalisation** as the semantic write gate, and both are scored by the
**same** ``longmemeval_benchmark._ranking_summary`` used for the hybrid run.
Neither forms memories, so neither is limited by formation coverage -- which is
exactly why the comparison has to be read carefully (see the emitted
``limitations``).

Durability
----------
The previous full-corpus sweep had no checkpoint: the runner kept everything in
``:memory:`` and only wrote at the end, so an interruption discarded the whole
run. This harness writes one JSONL line per (case, unit) as it goes and skips
already-finished pairs when ``--resume`` is passed, so an interrupted run is
resumable at ~10 s granularity.

Usage
-----
    python ops/longmemeval_naive_baseline.py <dataset> --output out.json \
        --unit both --device cuda --max-cases 10
    python ops/longmemeval_naive_baseline.py <dataset> --output out.json \
        --unit both --device cuda --resume
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dive_memory.eval_adapters.longmemeval import LongMemEvalAdapter, LongMemEvalCase  # noqa: E402
from dive_memory.eval_manifest import build_run_manifest  # noqa: E402
from dive_memory.longmemeval_benchmark import (  # noqa: E402
    _latency,
    _mean,
    _percentile,
    _ranking_summary,
)
from dive_memory.semantic_gate import (  # noqa: E402
    _cosine,
    _embed_many,
    _embed_one,
    load_local_gate,
)

RANK_KS = (1, 5, 10)


def _retrieval_units(case: LongMemEvalCase, unit: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Return ``(documents, turn_refs_per_doc, session_id_per_doc)`` for one case.

    ``turn`` keeps one document per non-empty turn. ``session`` joins every turn
    of a ``(session_id, occurrence)`` pair into one document -- the official
    session granularity. The occurrence key matters: thirteen official cases
    reuse a filler session id, and collapsing them would silently merge two
    distinct sessions into one retrieval unit.
    """
    nonempty = [turn for turn in case.turns if turn.content.strip()]
    if unit == "turn":
        return (
            [turn.content for turn in nonempty],
            [[turn.ref] for turn in nonempty],
            [turn.session_id for turn in nonempty],
        )
    if unit != "session":
        raise ValueError(f"unknown retrieval unit: {unit!r}")

    groups: dict[tuple[str, int], list[Any]] = {}
    order: list[tuple[str, int]] = []
    for turn in nonempty:
        key = (turn.session_id, turn.session_occurrence)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(turn)
    documents: list[str] = []
    refs: list[list[str]] = []
    sessions: list[str] = []
    for key in order:
        members = groups[key]
        documents.append("\n".join(turn.content for turn in members))
        refs.append([turn.ref for turn in members])
        sessions.append(members[0].session_id)
    return documents, refs, sessions


def _case_row(
    case: LongMemEvalCase,
    unit: str,
    *,
    encoder: Any,
    batch_size: int,
    top_k: int,
) -> dict[str, Any]:
    """Rank one case's retrieval units with plain cosine similarity."""
    documents, refs, sessions = _retrieval_units(case, unit)
    gold_turn_refs = {turn.ref for turn in case.turns if turn.has_answer}
    gold_session_ids = set(case.evidence_session_ids)

    encode_ms = 0.0
    rank_ms = 0.0
    order: list[int] = []
    if documents:
        started = perf_counter()
        vectors = _embed_many(encoder, documents, batch_size=batch_size)
        query_vector = _embed_one(encoder, case.question)
        encode_ms = (perf_counter() - started) * 1000
        rank_started = perf_counter()
        ranked = sorted(
            range(len(vectors)),
            key=lambda index: _cosine(query_vector, vectors[index]),
            reverse=True,
        )
        order = ranked[:top_k]
        rank_ms = (perf_counter() - rank_started) * 1000

    # The same two expressions serve both units: for ``turn`` each document
    # carries exactly one turn ref, for ``session`` it carries that session's
    # turn refs. Session hit is a set membership on the document's session id.
    turn_units = [sorted(set(refs[index]) & gold_turn_refs) for index in order]
    session_units = [
        [sessions[index]] if sessions[index] in gold_session_ids else []
        for index in order
    ]

    row: dict[str, Any] = {
        "question_id": case.question_id,
        "question_type": case.question_type,
        "should_abstain": case.should_abstain,
        "unit": unit,
        "top_k": top_k,
        "document_count": len(documents),
        "turn_gold_count": len(gold_turn_refs),
        "session_gold_count": len(gold_session_ids),
        "turn_relevant_by_rank": [bool(units) for units in turn_units],
        "session_relevant_by_rank": [bool(units) for units in session_units],
        "turn_relevant_units_by_rank": turn_units,
        "session_relevant_units_by_rank": session_units,
        "encode_ms": encode_ms,
        "rank_ms": rank_ms,
    }
    for k in RANK_KS:
        top = order[:k]
        seen_turns: set[str] = set()
        for index in top:
            seen_turns |= set(refs[index]) & gold_turn_refs
        seen_sessions = {sessions[index] for index in top}
        row[f"turn_coverage_at_{k}"] = (
            len(seen_turns) / len(gold_turn_refs) if gold_turn_refs else 0.0
        )
        row[f"session_coverage_at_{k}"] = (
            len(seen_sessions & gold_session_ids) / len(gold_session_ids)
            if gold_session_ids else 0.0
        )
    return row


def _rows_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".rows.jsonl")


def _load_done(rows_path: Path) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Read back a previous partial run so ``--resume`` can skip finished pairs."""
    rows: list[dict[str, Any]] = []
    done: set[tuple[str, str]] = set()
    if not rows_path.exists():
        return rows, done
    for line in rows_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A torn final line is the expected shape of a hard kill mid-write.
            continue
        row = record.get("row")
        if not isinstance(row, dict) or "question_id" not in row:
            continue
        rows.append(row)
        done.add((str(row["question_id"]), str(row["unit"])))
    return rows, done


def run_naive_baseline(
    dataset_path: str | Path,
    *,
    units: Sequence[str] = ("turn", "session"),
    max_cases: int | None = None,
    top_k: int = 10,
    repository_root: str | Path = ".",
    encoder: Any = None,
    gate_name: str = "NOT_APPLICABLE",
    batch_size: int = 16,
    resume_path: Path | None = None,
) -> dict[str, Any]:
    if encoder is None:
        raise ValueError("an encoder is required")
    cases = LongMemEvalAdapter.load(dataset_path)
    if max_cases is not None:
        cases = cases[: max(0, int(max_cases))]

    rows_path = resume_path
    rows, done = _load_done(rows_path) if rows_path is not None else ([], set())
    handle = rows_path.open("a", encoding="utf-8") if rows_path is not None else None

    started = datetime.now(timezone.utc)
    errors: list[dict[str, str]] = []
    try:
        for case in cases:
            for unit in units:
                if (case.question_id, unit) in done:
                    continue
                try:
                    row = _case_row(
                        case, unit, encoder=encoder, batch_size=batch_size, top_k=top_k
                    )
                except Exception as exc:  # keep per-case failure evidence
                    errors.append({
                        "question_id": case.question_id,
                        "unit": unit,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    })
                    continue
                rows.append(row)
                done.add((case.question_id, unit))
                if handle is not None:
                    handle.write(json.dumps({"row": row}, ensure_ascii=False) + "\n")
                    handle.flush()
    finally:
        if handle is not None:
            handle.close()

    completed = datetime.now(timezone.utc)
    configuration = {
        "baseline": "naive-dense-cosine",
        "units": list(units),
        "encoder": "local bge-m3 (BAAI/bge-m3, revision 5617a9f61b028005a4858fdac845db406aefb181)",
        "encoding": (
            "identical to the semantic write gate: _embed_many over the same "
            "checkpoint with max_seq_length pinned and head+tail truncation of "
            "long inputs, L2-normalised, cosine = dot product"
        ),
        "ranking": "global cosine sort over that case's own retrieval units; no fusion, no filters, no temporal predicate",
        "top_k": top_k,
        "max_cases": max_cases,
        "gold_visibility": "question used as the query; has_answer/answer_session_ids used for scoring only",
        "metric_code": "shared with the hybrid run (longmemeval_benchmark._ranking_summary)",
        "gate": gate_name,
    }
    manifest = build_run_manifest(
        run_id=f"longmemeval-naive-baseline-{started.strftime('%Y%m%dT%H%M%SZ')}",
        dataset_name="LongMemEval_S-cleaned",
        dataset_version="hf-98d7416c24c778c2fee6e6f3006e7a073259d48f",
        dataset_path=dataset_path,
        configuration=configuration,
        repository_root=repository_root,
    ).to_dict()

    report: dict[str, Any] = {
        "schema_version": "dive-longmemeval-naive-baseline-v1",
        "status": "COMPLETED",
        "started_at": started.isoformat(),
        "finished_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "manifest": manifest,
        "dataset_cases": len(cases),
        "errors": errors,
        "timeouts": [],
        "results": {},
        "limitations": [
            "These are protocol-compatible baselines written for this repository, not the official LongMemEval reference implementation.",
            "No reader and no GPT-4o judge ran; this is the retrieval stage only, so no QA/quality score is claimed.",
            "The baselines retrieve raw turn/session text, while the hybrid run retrieves formed memories. Neither is limited by formation coverage; the hybrid run is.",
            "Cosine ranking is exact and brute-force over the case's own units; it is not an ANN result.",
            "Duplicate filler session ids are kept as distinct retrieval units by (session_id, occurrence).",
        ],
    }
    for unit in units:
        unit_rows = [row for row in rows if row["unit"] == unit]
        report["results"][unit] = {
            "cases": len(unit_rows),
            "answerable_cases": sum(1 for row in unit_rows if not row["should_abstain"]),
            "documents_total": sum(int(row["document_count"]) for row in unit_rows),
            "turn_level": _ranking_summary(unit_rows, unit="turn"),
            "session_level": _ranking_summary(unit_rows, unit="session"),
            "encode_ms": _latency([float(row["encode_ms"]) for row in unit_rows]),
            "rank_ms": _latency([float(row["rank_ms"]) for row in unit_rows]),
        }
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in unit_rows:
            by_type[str(row["question_type"])].append(row)
        report["results"][unit]["by_question_type"] = {
            question_type: {
                "turn_level": _ranking_summary(group, unit="turn"),
                "session_level": _ranking_summary(group, unit="session"),
            }
            for question_type, group in sorted(by_type.items())
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset")
    parser.add_argument("--output", required=True)
    parser.add_argument("--unit", choices=("turn", "session", "both"), default="both")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--gate-max-sequence-length", type=int, default=512)
    parser.add_argument("--resume", action="store_true",
                        help="skip (case, unit) pairs already present in the JSONL checkpoint")
    parser.add_argument("--no-checkpoint", action="store_true",
                        help="do not write the per-case JSONL checkpoint")
    args = parser.parse_args()

    units = ("turn", "session") if args.unit == "both" else (args.unit,)
    output = Path(args.output)
    rows_path = None if args.no_checkpoint else _rows_path(output)
    if rows_path is not None and not args.resume and rows_path.exists():
        rows_path.unlink()

    gate = load_local_gate(
        device=None if args.device == "cpu" else args.device,
        cache_size=0,
        encode_batch_size=max(1, args.batch_size),
        max_sequence_length=max(1, args.gate_max_sequence_length),
    )
    report = run_naive_baseline(
        args.dataset,
        units=units,
        max_cases=args.max_cases,
        top_k=max(1, args.top_k),
        repository_root=Path.cwd(),
        encoder=gate.index.encoder,
        batch_size=max(1, args.batch_size),
        resume_path=rows_path,
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        unit: {
            "cases": report["results"][unit]["cases"],
            "turn_recall_at_10": report["results"][unit]["turn_level"]["recall_any_at_10"],
            "session_recall_at_10": report["results"][unit]["session_level"]["recall_any_at_10"],
        }
        for unit in units
    }, indent=2))


if __name__ == "__main__":
    main()
