"""Prepare and audit a *paired* Oracle v2/v3 reader pilot.

The planner writes only question IDs for the runner. The comparator never
calls a model and never reads reference answers. It refuses to call a reader
ablation comparable unless the recorded model, retrieval configuration, packed
evidence, and actual reader-visible context agree case by case. Saved local
judge verdicts are optional; without them there is no accuracy estimate.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import random
from statistics import median
import sys
from typing import Any, Mapping, Sequence

# Keep this standalone ops script usable from a source checkout without an
# editable install. The runner itself is normally invoked as a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dive_memory.oracle.contracts import CANONICAL_ABSTENTION_HYPOTHESIS
from dive_memory.oracle.loader import is_abstention, load_oracle_dataset, stratified_selection
from dive_memory.token_budget import ApproximateTokenCounter

SCHEMA_VERSION = "dive-oracle-reader-pair-v1"
BASELINE_POLICY = "evidence_first_v2"
CANDIDATE_POLICY = "evidence_first"
PROMPTS = {
    BASELINE_POLICY: "oracle-grounded-reader-v2",
    CANDIDATE_POLICY: "oracle-grounded-reader-v3",
}


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def plan_selection(cases: Sequence[Any], *, size: int, abstention: int,
                   seed: int) -> list[Any]:
    """Reserve unanswerable questions, then stratify the answerable remainder."""

    if size <= 0 or size > len(cases):
        raise ValueError("size must be between 1 and dataset size")
    if abstention < 0 or abstention > size:
        raise ValueError("abstention must be between 0 and size")
    unanswerable = sorted((case for case in cases if is_abstention(case.question_id)),
                          key=lambda case: case.question_id)
    answerable = [case for case in cases if not is_abstention(case.question_id)]
    if abstention > len(unanswerable) or size - abstention > len(answerable):
        raise ValueError("requested quota exceeds an available stratum")
    chosen_abs = random.Random(seed).sample(unanswerable, abstention)
    chosen_answerable = (stratified_selection(answerable, size=size - abstention,
                                              seed=seed) if size > abstention else [])
    return sorted([*chosen_abs, *chosen_answerable], key=lambda case: case.question_id)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_run(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = _read_json(root / "manifest.json")
    traces: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "traces").glob("*.json")):
        record = _read_json(path)
        qid = record.get("question_id")
        if not isinstance(qid, str) or not qid:
            raise ValueError(f"{path} has no question_id")
        if qid in traces:
            raise ValueError(f"duplicate question_id in {root / 'traces'}: {qid}")
        traces[qid] = record
    return manifest, traces


def _policy(manifest: Mapping[str, Any]) -> str | None:
    config = manifest.get("configuration") or {}
    pipeline = config.get("oracle_pipeline") or {}
    return pipeline.get("reader_policy")


def _reader_spec(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return dict((manifest.get("configuration") or {}).get("reader_spec") or {})


def _non_reader_configuration(manifest: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(manifest.get("configuration") or {})
    for field in ("reader_spec", "reader_fingerprint", "reader_policy",
                  "reader_policy_fingerprint"):
        config.pop(field, None)
    pipeline = dict(config.get("oracle_pipeline") or {})
    pipeline.pop("reader_policy", None)
    config["oracle_pipeline"] = pipeline
    return config


def _context_signature(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(record.get("context_text"), str) or not isinstance(
            record.get("reader_context_text"), str):
        return None
    retrieval = record.get("retrieval") or {}
    packing = record.get("packing") or {}
    return {
        "question_date": record.get("question_date"),
        "as_of": record.get("as_of"),
        "retrieved_ids": retrieval.get("returned_memory_ids"),
        "retrieved_refs": retrieval.get("returned_source_refs"),
        "packed_ids": packing.get("packed_memory_ids"),
        "packing_estimated_tokens": packing.get("estimated_tokens"),
        "packing_omitted": packing.get("omitted"),
        "packed_text_sha256": _text_digest(record["context_text"]),
        "reader_context_sha256": _text_digest(record["reader_context_text"]),
    }


def _reader_tokens(traces: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    rows = []
    for record in traces.values():
        usage = record.get("tokens") or {}
        if isinstance(usage.get("reader_input"), int):
            rows.append(int(usage["reader_input"]))
    return {"observed_cases": len(rows), "input_total": sum(rows),
            "input_mean": sum(rows) / len(rows) if rows else None,
            "note": "Provider/estimated input tokens include prompts and retries, not just context."}


def _load_verdicts(path: Path, traces: Mapping[str, Mapping[str, Any]],
                   dataset_sha: str) -> tuple[dict[str, Any], dict[str, bool | None]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if not lines or lines[0].get("_header") is not True:
        raise ValueError(f"{path} lacks a judge-verdict header")
    header = dict(lines[0])
    if header.get("dataset_sha256") != dataset_sha:
        raise ValueError(f"{path} judge dataset SHA differs from run")
    verdicts: dict[str, bool | None] = {}
    for row in lines[1:]:
        qid = row.get("question_id")
        if qid not in traces or qid in verdicts:
            raise ValueError(f"{path} has unknown or duplicate verdict ID {qid!r}")
        hypothesis = traces[qid].get("hypothesis")
        invalid = (str((traces[qid].get("reader") or {}).get("status")) in
                   {"INVALID", "ERROR", "TIMEOUT"} or
                   traces[qid].get("hypothesis_origin") == "ERROR_FALLBACK")
        null_failure = hypothesis is None and invalid and row.get("correct") is False
        if not isinstance(hypothesis, str) and not null_failure:
            raise ValueError(f"{path} verdict {qid!r} has no matching hypothesis")
        expected = _text_digest("null" if null_failure else hypothesis)[:16]
        if row.get("hypothesis_sha256") != expected:
            raise ValueError(f"{path} verdict {qid!r} does not match saved hypothesis")
        correct = row.get("correct")
        if correct is not None and not isinstance(correct, bool):
            raise ValueError(f"{path} verdict {qid!r} has non-boolean correctness")
        # A fallback string judged as a refusal must not reward a failed reader.
        verdicts[qid] = False if invalid else correct
    return header, verdicts


def _accuracy(traces_b: Mapping[str, Mapping[str, Any]],
              traces_c: Mapping[str, Mapping[str, Any]],
              verdicts_b: Mapping[str, bool | None],
              verdicts_c: Mapping[str, bool | None],
              question_types: Mapping[str, str] | None = None) -> dict[str, Any]:
    ids = sorted(set(traces_b) & set(traces_c))
    complete = all(isinstance(verdicts_b.get(qid), bool)
                   and isinstance(verdicts_c.get(qid), bool) for qid in ids)
    judged = [qid for qid in ids if isinstance(verdicts_b.get(qid), bool)
              and isinstance(verdicts_c.get(qid), bool)]
    transitions = Counter((verdicts_b[qid], verdicts_c[qid]) for qid in judged)

    def response_outcome(trace: Mapping[str, Any], correct: bool | None) -> str:
        """Return one mutually exclusive reader outcome for a judged case.

        Infrastructure failures take precedence over the fallback hypothesis.
        The runner writes canonical abstention text after a timeout or invalid
        response so the submission stays complete; that text is not evidence
        of a deliberate semantic refusal.
        """

        status = str((trace.get("reader") or {}).get("status") or "")
        origin = str(trace.get("hypothesis_origin") or "")
        if status in {"INVALID", "ERROR", "TIMEOUT"} or origin == "ERROR_FALLBACK":
            return "invalid_output"
        if correct is None:
            return "unjudged"
        refused = trace.get("hypothesis") == CANONICAL_ABSTENTION_HYPOTHESIS
        if refused:
            return "correct_refusal" if correct else "wrong_refusal"
        return "correct_answer" if correct else "wrong_answer"

    def arm(traces: Mapping[str, Mapping[str, Any]], verdicts: Mapping[str, bool | None]) -> dict[str, Any]:
        correct = sum(verdicts.get(qid) is True for qid in ids)
        abstain = sum(traces[qid].get("hypothesis") == CANONICAL_ABSTENTION_HYPOTHESIS
                      for qid in ids)
        abs_ids = [qid for qid in ids if is_abstention(qid)]
        correct_abs = sum(verdicts.get(qid) is True for qid in abs_ids)
        outcomes = Counter(response_outcome(traces[qid], verdicts.get(qid)) for qid in ids)
        return {"correct": correct, "full_set_accuracy": correct / len(ids) if complete and ids else None,
                "abstentions": abstain, "abstention_cases": len(abs_ids),
                "response_outcomes": {
                    name: outcomes[name] for name in (
                        "correct_answer", "wrong_answer", "correct_refusal",
                        "wrong_refusal", "invalid_output", "unjudged",
                    )
                },
                "correct_on_abstention_cases": correct_abs,
                "correct_refusals_on_abstention_cases": sum(
                    response_outcome(traces[qid], verdicts.get(qid)) == "correct_refusal"
                    for qid in abs_ids),
                "wrong_answers_on_abstention_cases": sum(
                    response_outcome(traces[qid], verdicts.get(qid)) == "wrong_answer"
                    for qid in abs_ids),
                "wrong_refusals_on_answerable_cases": sum(
                    response_outcome(traces[qid], verdicts.get(qid)) == "wrong_refusal"
                    for qid in ids if qid not in abs_ids)}

    by_type: dict[str, dict[str, Any]] = {}
    if question_types:
        for name in sorted(set(question_types.values())):
            group = [qid for qid in ids if question_types.get(qid) == name]
            if not group:
                continue
            group_complete = all(isinstance(verdicts_b.get(qid), bool)
                                 and isinstance(verdicts_c.get(qid), bool) for qid in group)
            b_correct = sum(verdicts_b.get(qid) is True for qid in group)
            c_correct = sum(verdicts_c.get(qid) is True for qid in group)
            by_type[name] = {
                "cases": len(group), "judge_complete": group_complete,
                "baseline_correct": b_correct, "candidate_correct": c_correct,
                "baseline_full_set_accuracy": b_correct / len(group) if group_complete else None,
                "candidate_full_set_accuracy": c_correct / len(group) if group_complete else None,
                "candidate_only": sum(verdicts_b.get(qid) is False
                                      and verdicts_c.get(qid) is True for qid in group),
                "baseline_only": sum(verdicts_b.get(qid) is True
                                     and verdicts_c.get(qid) is False for qid in group),
                "baseline_response_outcomes": dict(Counter(
                    response_outcome(traces_b[qid], verdicts_b.get(qid)) for qid in group
                )),
                "candidate_response_outcomes": dict(Counter(
                    response_outcome(traces_c[qid], verdicts_c.get(qid)) for qid in group
                )),
            }
    return {
        "score_type": "SELF_REPORTED_NON_OFFICIAL",
        "judge_complete": complete,
        "paired_cases": len(ids),
        "both_judged": len(judged),
        "baseline": arm(traces_b, verdicts_b),
        "candidate": arm(traces_c, verdicts_c),
        "paired_transitions": {
            "both_correct": transitions[(True, True)],
            "baseline_only": transitions[(True, False)],
            "candidate_only": transitions[(False, True)],
            "both_wrong": transitions[(False, False)],
        },
        "by_question_type": by_type,
        "note": "Accuracy requires complete saved verdicts from one identical local judge. This is never the official 40-point score.",
    }


def compare_runs(baseline_root: Path, candidate_root: Path,
                 *, baseline_verdicts: Path | None = None,
                 candidate_verdicts: Path | None = None,
                 dataset_path: Path | None = None) -> dict[str, Any]:
    b_manifest, b_traces = _load_run(baseline_root)
    c_manifest, c_traces = _load_run(candidate_root)
    issues: list[str] = []
    b_dataset = (b_manifest.get("dataset") or {}).get("sha256")
    c_dataset = (c_manifest.get("dataset") or {}).get("sha256")
    if not b_dataset or b_dataset != c_dataset:
        issues.append("dataset SHA differs or is missing")
    if _policy(b_manifest) != BASELINE_POLICY or _policy(c_manifest) != CANDIDATE_POLICY:
        issues.append("reader policies must be evidence_first_v2 versus evidence_first")
    b_spec, c_spec = _reader_spec(b_manifest), _reader_spec(c_manifest)
    if b_spec.get("prompt_version") != PROMPTS[BASELINE_POLICY] or c_spec.get(
            "prompt_version") != PROMPTS[CANDIDATE_POLICY]:
        issues.append("manifest reader prompt versions do not match the declared policies")
    b_model = {k: v for k, v in b_spec.items() if k != "prompt_version"}
    c_model = {k: v for k, v in c_spec.items() if k != "prompt_version"}
    if not b_model or b_model != c_model:
        issues.append("reader model/provider/parameters differ")
    if _non_reader_configuration(b_manifest) != _non_reader_configuration(c_manifest):
        issues.append("non-reader pipeline configurations differ")
    if (b_manifest.get("resume_key") or {}).get("selection_sha256") != (
            c_manifest.get("resume_key") or {}).get("selection_sha256"):
        issues.append("selection hashes differ")
    if set(b_traces) != set(c_traces) or not b_traces:
        issues.append("trace question ID sets differ or are empty")
    question_types: dict[str, str] = {}
    if dataset_path is not None:
        dataset = load_oracle_dataset(dataset_path)
        if dataset.sha256 != b_dataset or dataset.sha256 != c_dataset:
            issues.append("evaluation-side dataset SHA differs from run")
        question_types = {case.question_id: case.question_type for case in dataset.cases}
        if not set(b_traces) <= set(question_types) or not set(c_traces) <= set(question_types):
            issues.append("run includes question IDs outside evaluation-side dataset")
    for label, manifest in (("baseline", b_manifest), ("candidate", c_manifest)):
        snapshot = (manifest.get("configuration") or {}).get("code_snapshot") or {}
        snapshot_body = {key: value for key, value in snapshot.items() if key != "sha256"}
        expected_snapshot = _text_digest(json.dumps(snapshot_body, ensure_ascii=False,
                                                    sort_keys=True, separators=(",", ":")))
        has_snapshot = bool(snapshot.get("files") and snapshot.get("sha256") == expected_snapshot)
        if snapshot and not has_snapshot:
            issues.append(f"{label}: source snapshot is malformed or hash does not match")
        if not (manifest.get("git") or {}).get("commit") and not has_snapshot:
            issues.append(f"{label}: missing code identity")
        if (manifest.get("git") or {}).get("dirty") is True and not has_snapshot:
            issues.append(f"{label} run used a dirty worktree; code identity is not provable")
    b_commit = (b_manifest.get("git") or {}).get("commit")
    c_commit = (c_manifest.get("git") or {}).get("commit")
    if b_commit and c_commit and b_commit != c_commit:
        issues.append("run commits differ")

    counter = ApproximateTokenCounter()
    budget = ((_non_reader_configuration(b_manifest).get("oracle_pipeline") or {})
              .get("token_budget"))
    evidence_rows: list[dict[str, Any]] = []
    for qid in sorted(set(b_traces) & set(c_traces)):
        before, after = b_traces[qid], c_traces[qid]
        signature_b, signature_c = _context_signature(before), _context_signature(after)
        if signature_b is None or signature_c is None:
            issues.append(f"{qid}: actual reader context is missing")
        spec_b = ((before.get("reader") or {}).get("spec") or {}).get("prompt_version")
        spec_c = ((after.get("reader") or {}).get("spec") or {}).get("prompt_version")
        if spec_b != PROMPTS[BASELINE_POLICY] or spec_c != PROMPTS[CANDIDATE_POLICY]:
            issues.append(f"{qid}: trace prompt versions disagree with v2/v3 assignment")
        if signature_b != signature_c:
            issues.append(f"{qid}: retrieved/packed/reader-visible evidence differs")
        reader_text = before.get("reader_context_text")
        reader_tokens = counter.count(reader_text) if isinstance(reader_text, str) else None
        if isinstance(reader_tokens, int) and isinstance(budget, int) and reader_tokens > budget:
            issues.append(f"{qid}: reader-visible evidence exceeds the configured budget")
        packing_estimate = (before.get("packing") or {}).get("estimated_tokens")
        evidence_rows.append({
            "question_id": qid,
            "question_type": question_types.get(qid),
            "evidence_equal": signature_b is not None and signature_b == signature_c,
            "reader_context_sha256": signature_b.get("reader_context_sha256") if signature_b else None,
            "reader_context_heuristic_tokens": reader_tokens,
            "packing_estimated_tokens": packing_estimate,
            "context_over_budget_heuristic": (
                reader_tokens > budget if isinstance(reader_tokens, int)
                and isinstance(budget, int) else None),
            "baseline_reader_input_tokens": (before.get("tokens") or {}).get("reader_input"),
            "candidate_reader_input_tokens": (after.get("tokens") or {}).get("reader_input"),
            "baseline_reader_status": (before.get("reader") or {}).get("status"),
            "candidate_reader_status": (after.get("reader") or {}).get("status"),
        })

    # A verdict is not meaningful unless it belongs to the exact hypothesis
    # that this run produced. This check also prevents stale judge cache reuse.
    accuracy: dict[str, Any] | None = None
    if (baseline_verdicts is None) != (candidate_verdicts is None):
        issues.append("both verdict files must be supplied together")
    elif baseline_verdicts and candidate_verdicts:
        b_header, b_verdicts = _load_verdicts(baseline_verdicts, b_traces, str(b_dataset))
        c_header, c_verdicts = _load_verdicts(candidate_verdicts, c_traces, str(c_dataset))
        for header in (b_header, c_header):
            header.pop("_header", None)
        if b_header != c_header:
            issues.append("judge headers differ; verdicts are not same-judge paired")
        if not issues:
            accuracy = _accuracy(b_traces, c_traces, b_verdicts, c_verdicts,
                                 question_types)

    return {
        "schema_version": SCHEMA_VERSION,
        "score_type": "SELF_REPORTED_NON_OFFICIAL",
        "baseline_run": str(baseline_root),
        "candidate_run": str(candidate_root),
        "dataset_sha256": b_dataset,
        "comparable_reader_ablation": not issues,
        "issues": issues,
        "cases_compared": len(evidence_rows),
        "evidence_equal_cases": sum(row["evidence_equal"] for row in evidence_rows),
        "context_over_budget_heuristic_cases": [
            row["question_id"] for row in evidence_rows
            if row["context_over_budget_heuristic"] is True],
        "reader_input_tokens": {
            "baseline": _reader_tokens(b_traces),
            "candidate": _reader_tokens(c_traces),
        },
        "per_case": evidence_rows,
        "accuracy": accuracy,
        "official_score": "NOT_EVALUATED",
        "interpretation": (
            "Evidence equality and budget audit do not measure answer correctness. "
            "The reader-context token count is a rough character heuristic; "
            "provider reader-input tokens include instructions and retries. "
            "A dirty run without a complete equal code snapshot or unequal context is diagnostic only."
        ),
    }


def audit_run_context(root: Path) -> dict[str, Any]:
    """Audit one saved run's *actual* reader context without any judge or gold."""

    manifest, traces = _load_run(root)
    budget = (((manifest.get("configuration") or {}).get("oracle_pipeline") or {})
              .get("token_budget"))
    if not isinstance(budget, int) or budget <= 0:
        raise ValueError(f"{root} has no positive token_budget in manifest")
    counter = ApproximateTokenCounter()
    rows: list[dict[str, Any]] = []
    for qid, record in sorted(traces.items()):
        reader_text = record.get("reader_context_text")
        packing_estimate = (record.get("packing") or {}).get("estimated_tokens")
        if not isinstance(reader_text, str) or not isinstance(packing_estimate, int):
            continue
        rows.append({
            "question_id": qid,
            "reader_context_heuristic_tokens": counter.count(reader_text),
            "packing_estimated_tokens": packing_estimate,
        })
    packed = [row["packing_estimated_tokens"] for row in rows]
    reader = [row["reader_context_heuristic_tokens"] for row in rows]
    excess = sorted(
        rows, key=lambda row: row["reader_context_heuristic_tokens"]
        - row["packing_estimated_tokens"], reverse=True)
    return {
        "schema_version": "dive-oracle-reader-context-audit-v1",
        "run": str(root),
        "run_id": manifest.get("run_id"),
        "dataset_sha256": (manifest.get("dataset") or {}).get("sha256"),
        "git": manifest.get("git"),
        "prompt_version": _reader_spec(manifest).get("prompt_version"),
        "configured_context_budget": budget,
        "token_counter": counter.name,
        "token_counter_exact": counter.exact,
        "trace_count": len(traces),
        "auditable_cases": len(rows),
        "packing_estimated_tokens": {
            "median": median(packed) if packed else None,
            "max": max(packed) if packed else None,
            "over_budget_cases": sum(value > budget for value in packed),
        },
        "reader_context_heuristic_tokens": {
            "median": median(reader) if reader else None,
            "max": max(reader) if reader else None,
            "over_budget_cases": sum(value > budget for value in reader),
        },
        "largest_gaps": [{**row, "gap": row["reader_context_heuristic_tokens"]
                          - row["packing_estimated_tokens"]} for row in excess[:5]],
        "provider_reported_reader_input": _reader_tokens(traces),
        "score_type": "NO_ACCURACY_MEASURED",
        "official_score": "NOT_EVALUATED",
        "interpretation": (
            "Historical trace-only audit. Both counts use the project's approximate "
            "character heuristic, not provider-exact context tokens. Provider input "
            "usage includes instructions and retries. A larger rendered reader context "
            "indicates a budget/renderer audit issue, not a corrected benchmark score."
        ),
    }


def _write_same_or_new(path: Path, value: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") != value:
        raise ValueError(f"refusing to overwrite differing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="write a fixed, answer-free ID pilot")
    plan.add_argument("--dataset", required=True)
    plan.add_argument("--size", type=int, required=True)
    plan.add_argument("--abstention", type=int, required=True)
    plan.add_argument("--seed", type=int, default=20260924)
    plan.add_argument("--ids-output", required=True)
    compare = sub.add_parser("compare", help="audit two saved v2/v3 runs")
    compare.add_argument("--baseline-run", required=True)
    compare.add_argument("--candidate-run", required=True)
    compare.add_argument("--baseline-verdicts")
    compare.add_argument("--candidate-verdicts")
    compare.add_argument("--dataset", help="evaluation-side Oracle file for by-type metrics")
    compare.add_argument("--output", required=True)
    audit = sub.add_parser("audit", help="summarize one run's actual reader context budget")
    audit.add_argument("--run", required=True)
    audit.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        dataset = load_oracle_dataset(args.dataset)
        cases = plan_selection(dataset.cases, size=args.size,
                               abstention=args.abstention, seed=args.seed)
        ids = "".join(f"{case.question_id}\n" for case in cases)
        target = Path(args.ids_output)
        _write_same_or_new(target, ids)
        summary = {
            "schema_version": "dive-oracle-reader-pilot-plan-v1",
            "dataset_sha256": dataset.sha256,
            "size": len(cases), "seed": args.seed,
            "abstention_cases": sum(is_abstention(case.question_id) for case in cases),
            "question_type_counts": dict(sorted(Counter(case.question_type for case in cases).items())),
            "ids_sha256": _text_digest(ids),
            "ids_file": str(target),
            "note": "Only IDs go to the runner; type labels and gold answers are never in reader prompts.",
        }
        _write_same_or_new(target.with_suffix(target.suffix + ".plan.json"),
                           json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "audit":
        report = audit_run_context(Path(args.run))
        _write_same_or_new(Path(args.output),
                           json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    report = compare_runs(
        Path(args.baseline_run), Path(args.candidate_run),
        baseline_verdicts=Path(args.baseline_verdicts) if args.baseline_verdicts else None,
        candidate_verdicts=Path(args.candidate_verdicts) if args.candidate_verdicts else None,
        dataset_path=Path(args.dataset) if args.dataset else None,
    )
    target = Path(args.output)
    _write_same_or_new(target, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in (
        "comparable_reader_ablation", "cases_compared", "evidence_equal_cases",
        "context_over_budget_heuristic_cases", "accuracy", "issues", "official_score")},
        ensure_ascii=False, indent=2))
    return 0 if report["comparable_reader_ablation"] else 2


if __name__ == "__main__":
    sys.exit(main())
