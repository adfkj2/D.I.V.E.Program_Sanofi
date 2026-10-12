"""Independent, offline checks of frozen full-run answers, citations and scores."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from dive_memory.oracle.contracts import CANONICAL_ABSTENTION_HYPOTHESIS
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.runner import atomic_write_json
from dive_memory.eval_manifest import code_snapshot
from oracle_reader_pair import _load_run, _load_verdicts, audit_run_context
from oracle_v4_experiment import evidence_audit
from oracle_calculation_experiment import budget_ledgers
from oracle_currency_budget import peak_cost


def visible_passages(trace):
    text = trace["reader_context_text"]
    views = trace["evidence"]["views"]
    passages = {}
    offsets = []
    for view in views:
        marker = f"[{view['index']}] id={view['memory_id']}; source="
        start = text.find(marker)
        if start < 0 or text.find(marker, start + 1) >= 0:
            raise ValueError("Ambiguous or missing evidence header")
        body = text.find("\n    ", start)
        if body < 0:
            raise ValueError("Evidence body missing")
        offsets.append((start, body + 5, view))
    if offsets != sorted(offsets, key=lambda row: row[0]):
        raise ValueError("Evidence order differs from view indices")
    for i, (_, body, view) in enumerate(offsets):
        end = offsets[i + 1][0] - 1 if i + 1 < len(offsets) else len(text)
        passages[view["memory_id"]] = (text[body:end], set(view["source_refs"]))
    return passages


def audit_citations(trace):
    passages = visible_passages(trace)
    reader = trace["reader"]
    stages = json.loads(reader["detail"])["stages"]
    count = 0
    for outcome in [reader, *stages]:
        citations = outcome.get("citations", [])
        if outcome["status"] == "ANSWER" and not citations:
            raise ValueError("Answer lacks citations")
        if outcome["status"] != "ANSWER" and citations:
            raise ValueError("Non-answer contains citations")
        for citation in citations:
            quote = citation["quote"]
            passage, refs = passages[citation["memory_id"]]
            if not quote.strip() or quote.strip() not in passage:
                raise ValueError("Quote not present in cited visible passage")
            if set(citation["source_refs"]) != refs:
                raise ValueError("Citation provenance differs from visible passage")
            count += 1
    return count


def audit_submission(rows, traces):
    seen = set()
    for row in rows:
        if set(row) != {"question_id", "hypothesis"}:
            raise ValueError("Unexpected submission fields")
        qid = row["question_id"]
        if qid in seen or qid not in traces:
            raise ValueError("Duplicate or unknown submission ID")
        seen.add(qid)
        trace = traces[qid]
        expected = trace.get("hypothesis")
        if expected is None and trace["reader"]["status"] in {"INVALID", "ERROR", "TIMEOUT"}:
            expected = CANONICAL_ABSTENTION_HYPOTHESIS
        if row["hypothesis"] != expected:
            raise ValueError("Submission differs from saved answer")
    if seen != set(traces):
        raise ValueError("Submission incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / "eval/reports/oracle-adjudicated-full-20261004")
    args = parser.parse_args()
    root = args.run.resolve()
    frozen = json.loads((root / "frozen-plan.json").read_text(encoding="utf-8"))
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    if dataset.sha256 != frozen["dataset_sha256"]:
        raise ValueError("Dataset identity changed")
    manifest, traces = _load_run(root / "full-adjudicated")
    if set(traces) != set(dataset.question_ids):
        raise ValueError(f"Incomplete full run: {len(traces)}/500; no new accuracy claimed")
    previous = ROOT / "eval/reports/oracle-narrative-20260928-v2"
    _, old = _load_run(previous / "full-wide-indexed")
    pair = evidence_audit(old, traces)
    primary_mismatches = [qid for qid, trace in traces.items() if any(
        json.loads(trace["reader"]["detail"])["stages"][0][key] != old[qid]["reader"][key]
        for key in ("status", "hypothesis", "response_sha256"))]
    if primary_mismatches:
        raise ValueError("Cached primary responses differ from paired baseline")
    context = audit_run_context(root / "full-adjudicated")
    if context["auditable_cases"] != 500 or context["reader_context_heuristic_tokens"]["over_budget_cases"]:
        raise ValueError("Incomplete or over-budget context")
    citations = sum(audit_citations(trace) for trace in traces.values())
    submission = [json.loads(line) for line in
                  (root / "full-adjudicated/submission.jsonl").read_text(encoding="utf-8").splitlines()]
    audit_submission(submission, traces)
    bh, bv = _load_verdicts(previous / "full-verdicts.jsonl", old, dataset.sha256)
    ch, cv = _load_verdicts(root / "full-verdicts.jsonl", traces, dataset.sha256)
    if bh != ch or set(cv) != set(traces) or any(type(v) is not bool for v in cv.values()):
        raise ValueError("Judges differ or verdicts incomplete")
    improved = [qid for qid in traces if cv[qid] and not bv[qid]]
    regressed = [qid for qid in traces if bv[qid] and not cv[qid]]
    n = len(improved) + len(regressed)
    p = min(1.0, 2 * sum(math.comb(n, k) for k in range(min(len(improved), len(regressed)) + 1)) / 2**n)
    routes = defaultdict(Counter)
    for qid, trace in traces.items():
        route = json.loads(trace["reader"]["detail"])["route"]
        routes[route]["cases"] += 1
        routes[route]["correct"] += cv[qid]
        routes[route]["baseline_correct"] += bv[qid]
    invalid = [qid for qid, t in traces.items() if t["reader"]["status"] in {"INVALID", "ERROR", "TIMEOUT"}]
    saved_failures = json.loads((root / "full-adjudicated/failures.json").read_text(encoding="utf-8"))
    actual_failures = [t["failure"] for t in traces.values() if t.get("failure")]
    recovery = json.loads((root / "resume-plan-20261006.json").read_text(encoding="utf-8"))
    snapshot_matches = code_snapshot(ROOT) == frozen["source_snapshot"]
    recovery_matches = recovery["recovery_driver_sha256"] == sha256(
        (ROOT / "audit_tools/resume_adjudicated_full.py").read_bytes()).hexdigest()
    if not snapshot_matches or not recovery_matches:
        raise ValueError("Experiment or recovery source drift")
    result = {"audit_passed": True, "cases": 500, "score_type": "SELF_REPORTED_NON_OFFICIAL",
        "official_score": "NOT_EVALUATED", "baseline_correct": sum(bv.values()),
        "candidate_correct": sum(cv.values()), "improved": improved, "regressed": regressed,
        "paired_exact_sign_test_p": p, "paired_test_caveat": "Descriptive on reused public data; not blind validation.",
        "evidence_audit": pair, "primary_responses_identical_to_baseline": len(traces),
        "citation_checks_including_stages": citations,
        "invalid_ids_counted_wrong": invalid, "invalid_correct_count": sum(cv[qid] for qid in invalid),
        "routes": {route: dict(counts) for route, counts in routes.items()},
        "source_and_dependencies_unchanged": snapshot_matches,
        "recovery_source_unchanged": recovery_matches,
        "failure_inventory": {"actual": len(actual_failures), "saved": len(saved_failures),
            "matches": {f['question_id'] for f in actual_failures} == {f['question_id'] for f in saved_failures},
            "note": "Frozen runner omits checkpointed failures in its failure list; scored from all traces."},
        "aggregate_cost": peak_cost(budget_ledgers(root))}
    atomic_write_json(root / "post-run-audit-20261006.json", result)
    # Preserve the frozen runner's artifacts. Provide a separate complete list.
    atomic_write_json(root / "audited-failures-20261006.json", actual_failures)
    print(json.dumps({k: result[k] for k in ("audit_passed", "baseline_correct", "candidate_correct",
        "paired_exact_sign_test_p", "failure_inventory", "aggregate_cost")}), flush=True)


if __name__ == "__main__":
    main()
