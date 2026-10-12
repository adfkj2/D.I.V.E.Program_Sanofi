"""Offline paired retrieval diagnostic for the Oracle day-precision cutoff fix.

This compares preserved historical D-arm traces with a fresh deterministic-reader
run under the same semantic gate, document admission and balanced retrieval.
It measures evidence availability, not answer accuracy or a causal score gain.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dive_memory.eval_adapters.longmemeval import parse_instant
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.pipeline import PipelineConfig, run_case
from dive_memory.oracle.reader import DeterministicReader
from dive_memory.semantic_gate import DocumentAdmissionPolicy, load_local_gate


def _retrieved_gold(record: dict, gold_refs: list[str], *, packed: bool) -> bool:
    formed = (record.get("formation") or {}).get("turn_events") or {}
    events = {formed[ref] for ref in gold_refs if ref in formed}
    if packed:
        sources = [view.get("source_refs") or []
                   for view in (record.get("evidence") or {}).get("views") or []]
    else:
        sources = (record.get("retrieval") or {}).get("returned_source_refs") or []
    return any(events.intersection(refs) for refs in sources)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--historical-traces", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()

    dataset = load_oracle_dataset(args.dataset)
    historical = {}
    for path in Path(args.historical_traces).glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        historical[record["question_id"]] = record
    affected = []
    for case in dataset.cases:
        later_gold = [
            turn.ref for turn in case.turns
            if turn.has_answer
            and parse_instant(case.question_date_iso) < parse_instant(turn.occurred_at)
            <= parse_instant(case.visibility_cutoff_iso)
        ]
        if later_gold:
            affected.append((case, later_gold))
    selected = affected[:args.max_cases] if args.max_cases is not None else affected
    gate = load_local_gate(
        cache_size=1024, encode_batch_size=8,
        device=None if args.device == "cpu" else args.device,
        document_admission=DocumentAdmissionPolicy(mode="all"),
    )
    config = PipelineConfig(scoring_policy_name="balanced")
    rows = []
    for index, (case, later_gold) in enumerate(selected, 1):
        old = historical[case.question_id]
        new = run_case(case, reader=DeterministicReader(), gate=gate,
                       config=config, gate_name=gate.policy_version).record
        row = {
            "question_id": case.question_id,
            "should_abstain": case.should_abstain,
            "later_same_day_gold_turns": len(later_gold),
            "old_status": old.get("status"),
            "new_status": new.get("status"),
            "old_retrieved": (old.get("retrieval") or {}).get("returned"),
            "new_retrieved": (new.get("retrieval") or {}).get("returned"),
            "old_gold_retrieved": _retrieved_gold(old, list(case.evidence_refs), packed=False),
            "new_gold_retrieved": _retrieved_gold(new, list(case.evidence_refs), packed=False),
            "old_same_day_gold_retrieved": _retrieved_gold(old, later_gold, packed=False),
            "new_same_day_gold_retrieved": _retrieved_gold(new, later_gold, packed=False),
            "old_same_day_gold_packed": _retrieved_gold(old, later_gold, packed=True),
            "new_same_day_gold_packed": _retrieved_gold(new, later_gold, packed=True),
            "old_answer_origin": old.get("hypothesis_origin"),
        }
        rows.append(row)
        print(f"{index}/{len(selected)} {case.question_id} "
              f"same-day-gold: {row['old_same_day_gold_retrieved']} -> "
              f"{row['new_same_day_gold_retrieved']}", flush=True)

    def count(key: str) -> int:
        return sum(bool(row[key]) for row in rows)

    def answerable_count(key: str) -> int:
        return sum(bool(row[key]) for row in rows if not row["should_abstain"])

    report = {
        "schema_version": "oracle-temporal-diagnostic-v1",
        "dataset_sha256": dataset.sha256,
        "affected_cases_total": len(affected),
        "affected_later_gold_turns_total": sum(len(refs) for _, refs in affected),
        "cases_run": len(rows),
        "answerable_cases_run": sum(not row["should_abstain"] for row in rows),
        "abstention_cases_run": sum(row["should_abstain"] for row in rows),
        "historical_arm": "D; model reader v2; old exact-instant cutoff",
        "current_arm": "deterministic reader; current day-precision cutoff",
        "controlled": "semantic gate, document admission all, balanced scoring, dataset",
        "limitation": (
            "Historical vs current code and different readers. Only evidence-availability "
            "fields are compared; no causal QA accuracy or official score is inferred."
        ),
        "retrieval": {
            key: count(key) for key in (
                "old_gold_retrieved", "new_gold_retrieved",
                "old_same_day_gold_retrieved", "new_same_day_gold_retrieved",
                "old_same_day_gold_packed", "new_same_day_gold_packed",
            )
        },
        "answerable_retrieval": {
            key: answerable_count(key) for key in (
                "old_gold_retrieved", "new_gold_retrieved",
                "old_same_day_gold_retrieved", "new_same_day_gold_retrieved",
                "old_same_day_gold_packed", "new_same_day_gold_packed",
            )
        },
        "rows": rows,
    }
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "rows"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
