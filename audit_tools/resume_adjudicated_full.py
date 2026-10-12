"""Resume an interrupted frozen experiment without changing its implementation.

This recovery entry point is intentionally outside src/ops: those directories
are frozen in the existing runner manifests. Its own SHA is saved separately.
Only uncompleted, non-screen cases are projected as new reader work; judging
reserves all cases without verified cached verdicts. No source check is relaxed.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import getpass
from hashlib import sha256
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
import oracle_adjudicated_full as full


def resumable_trace(trace):
    # A completed malformed output is a scored failure, not unfinished work.
    # Preserve it as wrong; never resample it to improve the measured result.
    return (trace.get("status") == "COMPLETED"
            or (trace.get("status") == "DEGRADED"
                and trace.get("reader", {}).get("status") == "INVALID"))


def remaining_projection(screen_traces, answerable_ids, all_ids, completed_ids, screen_ids,
                         cached_judged_ids=()):
    expected = set(all_ids)
    completed, screened = set(completed_ids), set(screen_ids)
    judged = set(cached_judged_ids)
    if not completed <= expected or not screened <= expected or not judged <= screened:
        raise full.PilotStopped("Unexpected saved case IDs; refusing resume.")
    remaining = expected - completed - screened
    cost = full.projected_cost(screen_traces, answerable_ids, len(remaining))
    # Reader cache availability does not imply that the final answer was judged.
    judge_cases = len(expected - judged)
    judge = Decimal(3024) / 1000000 * judge_cases
    estimate = Decimal(cost["reader_review_cny"]) + judge
    cost.update(judge_allowance_cny=str(judge), estimated_additional_cny=str(estimate),
                planning_allowance_cny=str(estimate * Decimal("1.25")),
                completed_cases=len(completed), screen_cached_cases=len(screened),
                remaining_runner_cases=len(expected - completed),
                remaining_uncached_reader_cases=len(remaining),
                judge_allowance_cases=judge_cases)
    return cost


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "eval/reports/oracle-adjudicated-full-20261004")
    parser.add_argument("--api-key-env", default="DIVE_READER_API_KEY")
    args = parser.parse_args()
    root = args.output.resolve()
    frozen = full.read_json(root / "frozen-plan.json")
    snapshot = full.code_snapshot(ROOT)
    if snapshot != frozen["source_snapshot"]:
        raise full.PilotStopped("Frozen source or dependency drift; resume forbidden.")
    dataset = full.load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    dataset.require_full_benchmark()
    if (dataset.sha256 != frozen["dataset_sha256"]
            or full.CONFIG.to_dict() != frozen["config"]
            or full.REVIEW_VERSION != frozen["reader_version"]):
        raise full.PilotStopped("Frozen dataset or reader configuration differs.")
    plan = full.read_json(full.SCREEN / "frozen-plan.json")
    screen = full._load_run(full.SCREEN / "screen-refusal-gated")[1]
    _, verdicts = full._load_verdicts(full.SCREEN / "screen-verdicts.jsonl", screen, dataset.sha256)
    cases = [dataset.by_id()[qid] for qid in plan["ids"]]
    report = full.summarize(cases, screen, verdicts, exposed_ids=plan["ids"])
    if (set(verdicts) != set(plan["ids"])
            or not full.eligible(full.read_json(full.SCREEN / "baseline-evaluation.json"), report)):
        raise full.PilotStopped("Screen is no longer complete or eligible.")
    manifest, completed = full._load_run(root / "full-adjudicated")
    if manifest["configuration"]["code_snapshot"] != snapshot:
        raise full.PilotStopped("Saved reader source differs from frozen source.")
    old = full._load_run(full.PREVIOUS / "full-wide-indexed")[1]
    full.evidence_audit({qid: old[qid] for qid in screen}, screen)
    full.evidence_audit({qid: old[qid] for qid in completed}, completed)
    if any(not resumable_trace(t) for t in completed.values()):
        raise full.PilotStopped("Inspect incomplete saved traces before resuming.")
    ledger_paths = full.budget_ledgers(root)
    spent = full.peak_cost(ledger_paths)
    rules = full.read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json")
    cost = remaining_projection(screen, [c.question_id for c in cases if not c.should_abstain],
                                dataset.question_ids, completed, plan["ids"], verdicts)
    recovery = {"frozen_plan_sha256": sha256((root / "frozen-plan.json").read_bytes()).hexdigest(),
                "recovery_driver_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                "source_snapshot_sha256": snapshot["sha256"], "cost": cost, "spent": spent,
                "aggregate_budget": rules, "new_api_calls": 0,
                "note": "Budget estimate correction only; frozen reader, evidence and judge unchanged."}
    full.require_budget(cost, spent["peak_upper_cny"], rules)
    if not args.paid:
        full.atomic_write_json(root / "resume-readiness-20261006.json", recovery)
        print(full.json.dumps(recovery), flush=True)
        return
    key = os.environ.get(args.api_key_env) or getpass.getpass("API credential (hidden): ")
    if not key.strip() or full.MODEL not in [r["id"] for r in full.private_get("models", key).get("data", [])]:
        raise full.PilotStopped("Missing credential or model unavailable.")
    # A new balance checkpoint must measure only this continuation; the shared
    # currency ledger and original account-balance guard still cap cumulative use.
    guard = full.TotalBudgetGuard(key, root / "resume-balance-20261006",
        limit=Decimal(rules["planning_stop_cny"]) - Decimal(spent["peak_upper_cny"]))
    normal = full.OpenAICompatibleChatClient(full.ENDPOINT, key, provider="deepseek", extra_body=full.EXTRA)
    thinking = full.OpenAICompatibleChatClient(full.ENDPOINT, key, provider="deepseek", extra_body=full.THINKING)
    client = full.MeteredClient(normal, root / "api-cache", max_calls=1500, max_tokens=12000000,
        reuse_roots=[p.parent for p in ledger_paths if p.parent != root / "api-cache"],
        before_request=guard.check,
        currency_guard=full.CurrencyBudget(ledger_paths, limit=Decimal(rules["planning_stop_cny"])))
    policy = full.CONFIG.policy()
    primary = full.IndexedEvidenceReader(full.ModeClient(client, normal), full.ReaderSpec("deepseek", full.MODEL,
        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    secondary = full.IndexedEvidenceReader(full.ModeClient(client, thinking), full.ReaderSpec("deepseek", full.MODEL,
        prompt_version=policy.prompt_version, max_tokens=full.OUTPUT_CAP, timeout_seconds=120), policy=policy)
    referee = full.EvidenceReferee(full.ModeClient(client, normal), full.ReaderSpec("deepseek", full.MODEL,
        prompt_version=full.REFEREE_VERSION, max_tokens=1024), policy=policy)
    reader = full.AdjudicatedReader(primary, secondary, referee)
    if reader.spec.to_dict() != manifest["configuration"]["reader_spec"]:
        raise full.PilotStopped("Resume reader identity differs.")
    provider = full.model_provider(full.DEFAULT_MODEL, full.DEFAULT_REVISION, "cuda")
    gate, _ = full.make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = full.CachedEmbedder(provider)
    full.atomic_write_json(root / "resume-plan-20261006.json", recovery)
    initial_attempts = client.summary()["attempts"]
    try:
        offline = full.run(dataset, dataset.cases, root / "offline-before-full", gate, provider)
        full.atomic_write_json(root / "baseline-replay-audit.json", full.evidence_audit(old, offline))
        out = root / "full-adjudicated"
        result = full.run(dataset, dataset.cases, out, gate, provider, client, reader=reader)
        full.atomic_write_json(root / "full-pair-audit.json", full.evidence_audit(old, result))
        validation = full.validate_submission(out / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
        full.atomic_write_json(root / "submission-validation.json", validation.to_dict())
        if validation.status != "VALID":
            raise full.PilotStopped("Full submission validation failed.")
        client.client = normal
        full.judge_run(client, dataset, dataset.cases, out, root / "full-verdicts.jsonl")
        _, judged = full._load_verdicts(root / "full-verdicts.jsonl", result, dataset.sha256)
        report = full.summarize(dataset.cases, result, judged, exposed_ids=plan["ids"])
        full.atomic_write_json(root / "full-evaluation.json", report)
        _, old_verdicts = full._load_verdicts(full.PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
        full.atomic_write_json(root / "full-comparison.json", full._accuracy(old, result, old_verdicts, judged,
            {c.question_id: c.question_type for c in dataset.cases}))
        if not report["overall"]["fully_judged"] or full.code_snapshot(ROOT) != snapshot:
            raise full.PilotStopped("Incomplete judging or frozen source drift.")
        guard.check(client.summary()["attempts"], force=True)
        full.atomic_write_json(root / "completion.json", {"full_complete": True, "cases": 500,
            "target_reached": report["overall"]["correct"] >= 450, "official_score": "NOT_EVALUATED",
            "source_and_dependencies_unchanged": True,
            "new_api_calls_on_resume": client.summary()["attempts"] - initial_attempts})
        print(full.json.dumps(report["overall"]), flush=True)
    finally:
        full.atomic_write_json(root / "usage.json", client.summary())
        full.atomic_write_json(root / "aggregate-peak-cost.json", full.peak_cost(ledger_paths))


if __name__ == "__main__":
    try:
        main()
    except full.PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
