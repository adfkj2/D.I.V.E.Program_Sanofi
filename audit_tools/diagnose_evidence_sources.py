"""Offline source-level diagnostics; never feeds labels into answer generation."""
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.runner import atomic_write_json
from oracle_reader_pair import _load_run, _load_verdicts


def main():
    output = ROOT / "eval/reports/oracle-adjudicated-full-20261004"
    previous = ROOT / "eval/reports/oracle-narrative-20260928-v2"
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    _, traces = _load_run(previous / "full-wide-indexed")
    _, verdicts = _load_verdicts(previous / "full-verdicts.jsonl", traces, dataset.sha256)
    roles, failures = Counter(), Counter()
    role_rows, failure_rows = [], []
    for case in dataset.cases:
        trace = traces[case.question_id]
        formation = trace["formation"]
        # ref includes repeated-session occurrence; session_id alone is ambiguous.
        source_roles = {formation["turn_events"].get(turn.ref): turn.role for turn in case.turns}
        unknown_assistant = 0
        for view in trace["evidence"]["views"]:
            known = {source_roles.get(ref, "unknown") for ref in view["source_refs"]}
            roles["views"] += 1
            if view["role"] != "unknown":
                continue
            roles["unknown_views"] += 1
            if len(known) == 1 and "unknown" not in known:
                roles["unknown_but_provenance_unambiguous"] += 1
            if known == {"assistant"}:
                roles["unknown_actually_assistant"] += 1
                unknown_assistant += 1
            if known == {"user"}:
                roles["unknown_actually_user"] += 1
        if unknown_assistant:
            role_rows.append({"question_id": case.question_id, "unknown_assistant": unknown_assistant})
        if verdicts[case.question_id] or case.should_abstain:
            continue
        refs = [turn.ref for turn in case.turns if turn.has_answer]
        retrieved = {ref for group in trace["retrieval"]["returned_source_refs"] for ref in group}
        packed = {ref for view in trace["evidence"]["views"] for ref in view["source_refs"]}
        label = "all_gold_sources_visible_content_or_reasoning_issue"
        if not refs:
            label = "no_gold_turn_annotation"
        elif any(not formation["turn_memory_counts"].get(ref, 0) for ref in refs):
            label = "some_gold_source_not_formed"
        elif any(formation["turn_events"].get(ref) not in retrieved for ref in refs):
            label = "some_gold_source_not_retrieved"
        elif any(formation["turn_events"].get(ref) not in packed for ref in refs):
            label = "some_gold_source_not_packed"
        failures[label] += 1
        failure_rows.append({"question_id": case.question_id, "bucket": label,
                             "reader_status": trace["reader"]["status"]})
    atomic_write_json(output / "source-role-audit-20261006.json", {
        "is_accuracy": False, "cases": len(traces), "counts": dict(roles),
        "case_unknown_assistant": role_rows,
        "note": "Source-role metadata audit only; evaluator labels unused for role recovery."})
    atomic_write_json(output / "baseline-failure-source-audit-20261006.json", {
        "is_accuracy": False, "counts": dict(failures), "per_case": failure_rows,
        "note": "Evaluator-side diagnosis; source visibility does not establish visibility of the necessary sentence. Gold labels never enter reader routing."})
    print({"roles": dict(roles), "cases_unknown_assistant": len(role_rows),
           "failure_source_buckets": dict(failures)})


if __name__ == "__main__":
    main()
