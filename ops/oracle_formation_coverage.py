"""Gold-evidence coverage from Oracle run traces: formed, retrieved, packed.

Why this exists
---------------
The Oracle runner's traces already record, for *every* turn, how many memories
that turn produced (``formation.turn_memory_counts``), which provenance refs the
retrieval stage returned (``retrieval.returned_source_refs``) and which memories
survived packing into the reader's context (``evidence.views``). The gold turn
refs come from the dataset, which is evaluator-side and never enters a trace.

So the three stages *upstream* of the reader can be measured offline from an
existing run, with no model call and no API key. That matters because it is the
only way to compare a memory-side change against the published 500-question
DeepSeek run on equal terms when no reader credential is available.

Three nested levels
-------------------
``formed`` >= ``retrieved`` >= ``packed``. Each is an upper bound on the next:
a memory must exist before it can be returned, and be returned before it can be
packed. Reporting all three is what separates "the memory layer lost it" from
"the ranker lost it" from "the context budget lost it" -- which matters here,
because enabling document admission adds long memories that compete for the
same 1,500-token budget.

What it is not
--------------
This computes **no accuracy**. A formed, retrieved or packed memory is not a
correct answer. All three are upper bounds on what the reader could have used,
and the report says so in its ``interpretation`` block.

Unit check
----------
``returned_source_refs`` carries **event ids** (``evt_*``), not turn refs; the
gold turn ref is mapped through ``formation.turn_events``. This was verified
against the published run rather than assumed -- see the report's method note.

Gold handling
-------------
The gold refs come from ``LongMemEvalCase.evidence_refs``, i.e. the evaluator
side of the boundary. Nothing here is fed back into ingest or retrieval; this
script only reads artifacts after the fact.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "dive-oracle-coverage-v2"

LEVELS = ("formed", "retrieved", "packed")


def _iter_returned_refs(retrieval: Mapping[str, Any]) -> Iterable[str]:
    """Every provenance ref the retrieval stage returned, whatever the shape.

    The trace stores a list of per-item lists; older revisions stored a flat
    list. Both are accepted so a coverage comparison cannot be silently wrong
    because of a trace-format difference between two runs.
    """

    raw = retrieval.get("returned_source_refs")
    if not raw:
        return ()
    flat: list[str] = []
    for entry in raw:
        if isinstance(entry, (list, tuple, set)):
            flat.extend(str(item) for item in entry)
        else:
            flat.append(str(entry))
    return tuple(flat)


def _packed_source_refs(record: Mapping[str, Any]) -> set[str]:
    """Provenance refs whose memory survived packing into the reader's context.

    ``retrieval.returned_memory_ids`` and ``retrieval.returned_source_refs`` are
    parallel lists -- both are built by one comprehension over the same retrieved
    items -- so zipping them maps an event id to its memory id. Intersecting the
    result with ``packing.packed_memory_ids`` gives the refs that were actually
    handed to the reader.

    This path is used for *both* arms on purpose. The earlier alternative (read
    ``evidence.views``) is only available in newer traces, and a comparison that
    silently changed its unit between arms would not be a comparison.
    """

    retrieval = record.get("retrieval") or {}
    packing = record.get("packing") or {}
    memory_ids = list(retrieval.get("returned_memory_ids") or [])
    ref_groups = list(retrieval.get("returned_source_refs") or [])
    packed_ids = {str(item) for item in (packing.get("packed_memory_ids") or ())}
    if not memory_ids or not packed_ids:
        return set()
    refs: set[str] = set()
    for memory_id, group in zip(memory_ids, ref_groups):
        if str(memory_id) not in packed_ids:
            continue
        if isinstance(group, (list, tuple, set)):
            refs.update(str(item) for item in group)
        else:
            refs.add(str(group))
    return refs


def _coverage(formed: int, retrieved: int, gold: int, packed: int = 0) -> dict[str, Any]:
    return {
        "gold_turns": gold,
        "formed": formed,
        "retrieved": retrieved,
        "packed": packed,
        "formed_rate": (formed / gold) if gold else None,
        "retrieved_rate": (retrieved / gold) if gold else None,
        "packed_rate": (packed / gold) if gold else None,
        "retrieved_given_formed_rate": (retrieved / formed) if formed else None,
        "packed_given_retrieved_rate": (packed / retrieved) if retrieved else None,
    }


def _bucket() -> dict[str, int]:
    return {"cases": 0, "gold_turns": 0, "formed": 0, "retrieved": 0, "packed": 0}


def analyse_traces(
    dataset_path: Path,
    traces_dir: Path,
    *,
    expected_cases: int | None = None,
) -> dict[str, Any]:
    from dive_memory.oracle.loader import load_oracle_dataset

    dataset = load_oracle_dataset(dataset_path, expected_cases=expected_cases)
    per_type: dict[str, dict[str, int]] = defaultdict(_bucket)
    #: Same shape, but counting **every** case including the abstention ones.
    #: ``per_type`` deliberately excludes abstention cases (their gold is reported
    #: separately), which makes ``per_case`` irreconcilable with ``by_question_type``
    #: unless the excluded part is also published. See ``reconciliation`` below.
    per_type_all: dict[str, dict[str, int]] = defaultdict(_bucket)
    abstention_by_type: dict[str, dict[str, int]] = defaultdict(_bucket)
    per_case: list[dict[str, Any]] = []
    missing_traces: list[str] = []
    totals = _bucket()
    totals.update({"memories_formed": 0, "nonempty_turns": 0, "encoder_inputs": 0,
                   "context_tokens": 0, "packed_items": 0, "omitted_items": 0,
                   "cases_with_omissions": 0, "cases_with_empty_retrieval": 0})
    abstention = _bucket()
    failures: dict[str, int] = defaultdict(int)

    for case in dataset.cases:
        path = traces_dir / f"{case.question_id}.json"
        if not path.exists():
            missing_traces.append(case.question_id)
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        totals["cases"] += 1
        per_type[case.question_type]["cases"] += 1
        if case.should_abstain:
            abstention["cases"] += 1

        formation = record.get("formation") or {}
        counts: Mapping[str, Any] = formation.get("turn_memory_counts") or {}
        events: Mapping[str, Any] = formation.get("turn_events") or {}
        totals["memories_formed"] += int(formation.get("memories_formed") or 0)
        totals["nonempty_turns"] += int(formation.get("turns_ingested") or 0)
        totals["encoder_inputs"] += int(formation.get("encoder_inputs") or 0)

        retrieval = record.get("retrieval") or {}
        packing = record.get("packing") or {}
        omitted = int(packing.get("omitted") or 0)
        packed_items = int(packing.get("packed_count") or 0)
        totals["omitted_items"] += omitted
        totals["packed_items"] += packed_items
        totals["context_tokens"] += int(packing.get("estimated_tokens") or 0)
        if omitted:
            totals["cases_with_omissions"] += 1
        if retrieval.get("empty"):
            totals["cases_with_empty_retrieval"] += 1

        status = str(record.get("status"))
        if status and status != "COMPLETED":
            failures[status] += 1
        failure = record.get("failure")
        if isinstance(failure, dict) and failure.get("stage"):
            failures[f"STAGE_{failure['stage']}"] += 1

        returned = set(_iter_returned_refs(retrieval))
        packed_refs = _packed_source_refs(record)

        struck = {"formed": 0, "retrieved": 0, "packed": 0}
        all_bucket = per_type_all[case.question_type]
        all_bucket["cases"] += 1
        for ref in case.evidence_refs:
            bucket = abstention if case.should_abstain else per_type[case.question_type]
            bucket["gold_turns"] += 1
            all_bucket["gold_turns"] += 1
            type_abstention = None
            if case.should_abstain:
                type_abstention = abstention_by_type[case.question_type]
                type_abstention["gold_turns"] += 1
            event_id = str(events.get(ref) or "")
            hits = {
                "formed": int(counts.get(ref) or 0) > 0,
                "retrieved": bool(event_id) and event_id in returned,
                "packed": bool(event_id) and event_id in packed_refs,
            }
            for level in LEVELS:
                if hits[level]:
                    struck[level] += 1
                    bucket[level] += 1
                    all_bucket[level] += 1
                    if type_abstention is not None:
                        type_abstention[level] += 1
            if not case.should_abstain:
                totals["gold_turns"] += 1
                for level in LEVELS:
                    if hits[level]:
                        totals[level] += 1

        per_case.append({
            "question_id": case.question_id,
            "question_type": case.question_type,
            "should_abstain": case.should_abstain,
            "status": status,
            "gold_turns": len(case.evidence_refs),
            **struck,
            "memories_formed": int(formation.get("memories_formed") or 0),
            "retrieved_items": int(retrieval.get("returned") or 0),
            "packed_items": packed_items,
            "omitted_items": omitted,
            "context_tokens": int(packing.get("estimated_tokens") or 0),
            "reader_status": (record.get("reader") or {}).get("status"),
            "reader_reason": (record.get("reader") or {}).get("reason_code"),
        })

    scored_types: dict[str, dict[str, Any]] = {}
    for name, values in sorted(per_type.items()):
        entry: dict[str, Any] = dict(values)
        entry.update({
            "formed_rate": (values["formed"] / values["gold_turns"]) if values["gold_turns"] else None,
            "retrieved_rate": (values["retrieved"] / values["gold_turns"]) if values["gold_turns"] else None,
            "packed_rate": (values["packed"] / values["gold_turns"]) if values["gold_turns"] else None,
        })
        scored_types[name] = entry

    all_types: dict[str, dict[str, Any]] = {}
    for name in sorted(set(per_type_all) | set(per_type) | set(abstention_by_type)):
        values = per_type_all[name]
        entry: dict[str, Any] = dict(values)
        entry.update({
            "gold_turns_answerable": per_type[name]["gold_turns"],
            "gold_turns_abstention": abstention_by_type[name]["gold_turns"],
            "formed_rate_all_cases": (
                values["formed"] / values["gold_turns"] if values["gold_turns"] else None
            ),
        })
        all_types[name] = entry

    cases = totals["cases"] or 1
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": {
            "path": str(dataset_path),
            "sha256": dataset.sha256,
            "cases": len(dataset.cases),
        },
        "traces_dir": str(traces_dir),
        "overall": {
            **_coverage(totals["formed"], totals["retrieved"], totals["gold_turns"],
                        totals["packed"]),
            "cases": totals["cases"],
            "memories_formed": totals["memories_formed"],
            "nonempty_turns": totals["nonempty_turns"],
            "encoder_inputs": totals["encoder_inputs"],
            "memories_per_nonempty_turn": (
                totals["memories_formed"] / totals["nonempty_turns"]
                if totals["nonempty_turns"] else None
            ),
            "mean_context_tokens": totals["context_tokens"] / cases,
            "mean_packed_items": totals["packed_items"] / cases,
            "mean_retrieved_items": totals["packed_items"] / cases,
            "total_omitted_items": totals["omitted_items"],
            "cases_with_omissions": totals["cases_with_omissions"],
            "cases_with_empty_retrieval": totals["cases_with_empty_retrieval"],
        },
        "by_question_type": scored_types,
        "by_question_type_all_cases": all_types,
        "abstention_by_question_type": {
            name: dict(values) for name, values in sorted(abstention_by_type.items())
        },
        "reconciliation": {
            "gold_turns_all_cases": sum(v["gold_turns"] for v in per_type_all.values()),
            "gold_turns_answerable": totals["gold_turns"],
            "gold_turns_abstention": abstention["gold_turns"],
            "per_case_gold_counts_all_cases": True,
            "invariant": (
                "For every question type: "
                "by_question_type[t].gold_turns + abstention_by_question_type[t].gold_turns "
                "== by_question_type_all_cases[t].gold_turns, and summing "
                "per_case[].gold_turns over all cases equals gold_turns_all_cases."
            ),
            "note": (
                "per_case[].gold_turns counts every case, including the abstention ones, "
                "so it sums to gold_turns_all_cases. overall.gold_turns and "
                "by_question_type exclude abstention cases by design (their gold is "
                "reported in abstention_cases / abstention_by_question_type). Anyone "
                "cross-checking per_case against overall must reconcile the two, which is "
                "why both sides are published here."
            ),
        },
        "abstention_cases": _coverage(
            abstention["formed"], abstention["retrieved"], abstention["gold_turns"],
            abstention["packed"],
        ),
        "missing_traces": missing_traces,
        "trace_failures": dict(sorted(failures.items())),
        "per_case": per_case,
        "interpretation": {
            "is_accuracy": False,
            "levels_are_nested": True,
            "unit_note": (
                "returned_source_refs carries event ids; a gold turn ref is mapped "
                "through formation.turn_events. Verified against a published run."
            ),
            "note": (
                "Formation, retrieval and packing coverage are upper bounds on the "
                "answer stage. They say whether the evidence reached the reader's "
                "reach, never whether the reader answered correctly."
            ),
        },
    }


def summarize(report: Mapping[str, Any]) -> str:
    overall = report["overall"]
    lines = [
        f"cases: {overall['cases']}   gold turns: {overall['gold_turns']}",
        f"memories formed: {overall['memories_formed']} over "
        f"{overall['nonempty_turns']} non-empty turns "
        f"({overall['memories_per_nonempty_turn']:.3f}/turn)",
        f"context tokens (mean): {overall['mean_context_tokens']:.1f}   "
        f"packed items (mean): {overall['mean_packed_items']:.2f}   "
        f"omitted items (total): {overall['total_omitted_items']}",
        f"missing traces: {len(report['missing_traces'])}   "
        f"trace failures: {report['trace_failures']}",
        "",
        f"{'level':<12}{'count':>8}{'rate':>10}",
    ]
    for level in LEVELS:
        value = overall[level]
        rate = overall[f"{level}_rate"]
        lines.append(f"{level:<12}{value:>8}{(f'{rate:.4f}' if rate is not None else 'n/a'):>10}")
    lines.append("")
    lines.append(f"{'question_type':<28}{'cases':>6}{'gold':>6}"
                 f"{'formed':>8}{'rate':>9}{'packed':>8}{'rate':>9}")
    for name, values in report["by_question_type"].items():
        fr, pr = values["formed_rate"], values["packed_rate"]
        lines.append(
            f"{name:<28}{values['cases']:>6}{values['gold_turns']:>6}"
            f"{values['formed']:>8}{(f'{fr:.4f}' if fr is not None else 'n/a'):>9}"
            f"{values['packed']:>8}{(f'{pr:.4f}' if pr is not None else 'n/a'):>9}"
        )
    rec = report.get("reconciliation")
    if rec:
        lines.append("")
        lines.append(
            f"gold turns: all cases {rec['gold_turns_all_cases']} = answerable "
            f"{rec['gold_turns_answerable']} + abstention {rec['gold_turns_abstention']}"
            "   (per_case counts every case; overall/by_question_type exclude abstention)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python ops/oracle_formation_coverage.py",
        description="Compute gold-evidence coverage from Oracle run traces.",
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--traces", required=True, help="run's traces/ directory")
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-partial", action="store_true",
                        help="permit a dataset smaller than the official 500 questions")
    args = parser.parse_args(argv)

    report = analyse_traces(
        Path(args.dataset),
        Path(args.traces),
        expected_cases=None if args.allow_partial else 500,
    )
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(summarize(report))
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
