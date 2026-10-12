"""Prepare or execute the promoted reader's full run under the shared budget.

Default is an offline cost/readiness report. --paid cannot raise the budget.
"""
import argparse
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from oracle_refusal_gated_experiment import (
    ModeClient, CONFIG, THINKING, OUTPUT_CAP, PREVIOUS, budget_ledgers, eligible,
    peak_cost, CurrencyBudget, MeteredClient, PilotStopped, ENDPOINT, EXTRA, MODEL,
    private_get, TotalBudgetGuard, _load_run, _load_verdicts, _accuracy, read_json,
    judge_run, summarize, CachedEmbedder, model_provider, make_gate, DEFAULT_MODEL,
    DEFAULT_REVISION, evidence_audit, AdjudicatedReader, EvidenceReferee,
    REFEREE_VERSION, REVIEW_VERSION, OpenAICompatibleChatClient, IndexedEvidenceReader,
    ReaderSpec, atomic_write_json, load_oracle_dataset, code_snapshot, run,
)
from dive_memory.oracle.validator import validate_submission

SCREEN = ROOT / "eval/reports/oracle-adjudicated-20261004"


def projected_cost(traces, answerable_ids, remaining):
    """Project uncached secondary/review usage; primary requests are cached.

    Include repair calls through aggregated stage usage. Judge allowance assumes
    1,000 input and 128 output tokens for every remaining case, even cache hits.
    This is a planning estimate, not a guaranteed bill or accuracy prediction.
    """
    inputs = outputs = 0
    for qid in answerable_ids:
        stages = json.loads(traces[qid]["reader"]["detail"])["stages"]
        for stage in stages[1:]:
            usage = stage["usage"]
            i, o = usage["input_tokens"], usage["output_tokens"]
            if any(type(x) is not int or x < 0 for x in (i, o)):
                raise PilotStopped("Unknown screening usage; cannot authorize full-run estimate.")
            inputs += i
            outputs += o
    if not answerable_ids:
        raise PilotStopped("No answerable development sample.")
    reader = Decimal(2 * inputs + 8 * outputs) / 1000000 * remaining / len(answerable_ids)
    judge = Decimal(3024) / 1000000 * remaining
    return {"remaining_cases": remaining, "sample_answerable_cases": len(answerable_ids),
            "reader_review_cny": str(reader), "judge_allowance_cny": str(judge),
            "estimated_additional_cny": str(reader + judge),
            "planning_allowance_cny": str((reader + judge) * Decimal("1.25")),
            "caveat": "Observed sample extrapolation plus 25 percent margin; not a guaranteed bound."}


def require_budget(cost, spent, rules):
    limit = Decimal(rules["planning_stop_cny"])
    if limit > Decimal(rules["user_limit_cny"]) - Decimal(rules["settlement_reserve_cny"]):
        raise PilotStopped("Planning limit does not preserve the authorized settlement reserve.")
    if Decimal(spent) + Decimal(cost["planning_allowance_cny"]) >= limit:
        raise PilotStopped("Full-run estimate exceeds the recorded shared budget; no paid request sent.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-adjudicated-full-20261004")
    parser.add_argument("--api-key-env", default="DIVE_READER_API_KEY")
    args = parser.parse_args()
    root = args.output.resolve()
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    dataset.require_full_benchmark()
    plan = read_json(SCREEN / "frozen-plan.json")
    snapshot = code_snapshot(ROOT)
    # Adding this driver is allowed; every source that produced the screen stays fixed.
    if (any(snapshot["files"].get(p) != digest for p, digest in plan["source_snapshot"]["files"].items())
            or any(snapshot[k] != plan["source_snapshot"][k] for k in ("python", "dependencies"))
            or dataset.sha256 != plan["dataset_sha256"]):
        raise PilotStopped("Screened implementation/environment changed; rescreen before full run.")
    traces = _load_run(SCREEN / "screen-refusal-gated")[1]
    _, verdicts = _load_verdicts(SCREEN / "screen-verdicts.jsonl", traces, dataset.sha256)
    cases = [dataset.by_id()[qid] for qid in plan["ids"]]
    report = summarize(cases, traces, verdicts, exposed_ids=plan["ids"])
    if set(verdicts) != set(plan["ids"]) or not eligible(read_json(SCREEN / "baseline-evaluation.json"), report):
        raise PilotStopped("Screen does not pass promotion.")
    remaining = [c for c in dataset.cases if c.question_id not in plan["ids"]]
    if any(c.should_abstain for c in remaining):
        raise PilotStopped("Cost model requires all unanswerable cases in the development screen.")
    ledgers = budget_ledgers(root)
    spent = peak_cost(ledgers)
    rules = read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json")
    cost = projected_cost(traces, [c.question_id for c in cases if not c.should_abstain], len(remaining))
    proposal = {"screen_score": report["overall"], "screen_promoted": True,
                "dataset_sha256": dataset.sha256, "source_snapshot": snapshot,
                "reader_version": REVIEW_VERSION, "config": CONFIG.to_dict(),
                "aggregate_budget": rules, "spent": spent, "cost": cost,
                "full_run": "NOT_RUN", "official_score": "NOT_EVALUATED"}
    atomic_write_json(root / "prepared-plan.json", proposal)
    if not args.paid:
        print(json.dumps({k: proposal[k] for k in ("screen_score", "spent", "cost", "full_run")}))
        return
    require_budget(cost, spent["peak_upper_cny"], rules)
    frozen = root / "frozen-plan.json"
    identity = {k: v for k, v in proposal.items() if k != "spent"}
    if frozen.exists() and read_json(frozen) != identity:
        raise PilotStopped("Frozen full-run plan changed; use a new directory.")
    atomic_write_json(frozen, identity)
    key = os.environ.get(args.api_key_env) or getpass.getpass("DeepSeek key (hidden, never saved): ")
    if not key.strip() or MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
        raise PilotStopped("Missing credential or unavailable model.")
    guard = TotalBudgetGuard(key, root, limit=Decimal(rules["planning_stop_cny"]) - Decimal(spent["peak_upper_cny"]))
    guard.check(0, force=True)
    normal = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA)
    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=THINKING)
    client = MeteredClient(normal, root / "api-cache", max_calls=1500, max_tokens=12000000,
        reuse_roots=[p.parent for p in ledgers if p.parent != root / "api-cache"],
        before_request=guard.check, currency_guard=CurrencyBudget(ledgers, limit=Decimal(rules["planning_stop_cny"])))
    policy = CONFIG.policy()
    primary = IndexedEvidenceReader(ModeClient(client, normal), ReaderSpec("deepseek", MODEL,
        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    secondary = IndexedEvidenceReader(ModeClient(client, thinking), ReaderSpec("deepseek", MODEL,
        prompt_version=policy.prompt_version, max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy)
    referee = EvidenceReferee(ModeClient(client, normal), ReaderSpec("deepseek", MODEL,
        prompt_version=REFEREE_VERSION, max_tokens=1024), policy=policy)
    reader = AdjudicatedReader(primary, secondary, referee)
    old = _load_run(PREVIOUS / "full-wide-indexed")[1]
    _, old_verdicts = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    try:
        offline = run(dataset, dataset.cases, root / "offline-before-full", gate, provider)
        atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(old, offline))
        out = root / "full-adjudicated"
        result = run(dataset, dataset.cases, out, gate, provider, client, reader=reader)
        atomic_write_json(root / "full-pair-audit.json", evidence_audit(old, result))
        validation = validate_submission(out / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
        atomic_write_json(root / "submission-validation.json", validation.to_dict())
        if validation.status != "VALID":
            raise PilotStopped("Full submission validation failed.")
        client.client = normal
        judge_run(client, dataset, dataset.cases, out, root / "full-verdicts.jsonl")
        _, judged = _load_verdicts(root / "full-verdicts.jsonl", result, dataset.sha256)
        full = summarize(dataset.cases, result, judged, exposed_ids=plan["ids"])
        atomic_write_json(root / "full-evaluation.json", full)
        atomic_write_json(root / "full-comparison.json", _accuracy(old, result, old_verdicts, judged,
            {c.question_id: c.question_type for c in dataset.cases}))
        if not full["overall"]["fully_judged"] or code_snapshot(ROOT) != snapshot:
            raise PilotStopped("Incomplete judging or source drift; not a completed experiment.")
        guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"full_complete": True, "cases": 500,
            "target_reached": full["overall"]["correct"] >= 450, "official_score": "NOT_EVALUATED"})
        print(json.dumps(full["overall"]))
    finally:
        atomic_write_json(root / "usage.json", client.summary())
        atomic_write_json(root / "aggregate-peak-cost.json", peak_cost(ledgers))


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
