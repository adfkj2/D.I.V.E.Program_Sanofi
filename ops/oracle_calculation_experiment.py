"""Frozen, fixed-evidence calculation ablation; offline preparation by default."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]

from oracle_low_cost_pilot import MeteredClient, PilotStopped, private_get, MODEL, ENDPOINT, EXTRA
from oracle_currency_budget import CurrencyBudget, peak_cost
from oracle_narrative_experiment import ARMS, TotalBudgetGuard
from oracle_structural_experiment import CachedEmbedder
from oracle_complete_test import judge_run, summarize, read_json
from oracle_reader_pair import _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_v4_experiment import OfflineReader, evidence_audit
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.calculating_reader import CalculatingReader, routes_to_calculator, VERSION
from dive_memory.oracle.reader import ReaderSpec
from dive_memory.oracle.providers import OpenAICompatibleChatClient, ScriptedChatClient
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, trace_filename
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.validator import validate_submission
from dive_memory.eval_manifest import code_snapshot

PREVIOUS = ROOT / "eval/reports/oracle-narrative-20260928-v2"
CONFIG = replace(ARMS["wide"], reader_policy_name="cited_calculation")
OUTPUT_CAP = 1024
PRIOR_RUNS = ("oracle-narrative-20260928", "oracle-narrative-20260928-v2",
              "oracle-reasoning-20260929", "oracle-context-repair-20260929")


def budget_ledgers(root):
    """Include every frozen run in this budget scope, independent of its name."""
    paths = {ROOT / "eval/reports" / name / "api-cache/ledger.json" for name in PRIOR_RUNS}
    paths.update((ROOT / "eval/reports").glob("oracle-calculation-*/api-cache/ledger.json"))
    rules = ROOT / "eval/reports/oracle-road-to-90-budget.json"
    if rules.exists():
        scope = read_json(rules).get("scope")
        if not isinstance(scope, str) or not scope:
            raise PilotStopped("Shared budget scope is missing; no paid request.")
        for frozen in (ROOT / "eval/reports").glob("*/frozen-plan.json"):
            previous_budget = read_json(frozen).get("aggregate_budget") or {}
            if previous_budget.get("scope") == scope:
                paths.add(frozen.parent / "api-cache/ledger.json")
    paths.add(root / "api-cache/ledger.json")
    return sorted(paths)


def eligible(baseline, candidate):
    # Keep the existing development promotion rule; missing verdicts fail it.
    return (candidate["overall"]["fully_judged"]
            and candidate["answerable"]["cases"] == 50
            and candidate["unanswerable"]["cases"] == 30
            and candidate["answerable"]["correct"] >= baseline["answerable"]["correct"] + 3
            and candidate["unanswerable"]["correct"] >= baseline["unanswerable"]["correct"])


def route_summary(cases):
    rows = [{"question_id": c.question_id, "routed": routes_to_calculator(c.question)} for c in cases]
    return {"cases": len(rows), "routed_cases": sum(r["routed"] for r in rows),
            "routing_inputs": ["question"], "is_accuracy": False, "per_case": rows}


def computation_summary(traces):
    counts = Counter()
    for trace in traces.values():
        reader = trace.get("reader") or {}
        errors = reader.get("attempt_errors") or []
        if any(e.get("reason_code") == "CALCULATION_FALLBACK" for e in errors):
            counts["fallback"] += 1
        elif reader.get("reason_code") == "CITED_CALCULATION_RESULT":
            counts["computed"] += 1
        else:
            counts["ordinary_answer_or_refusal"] += 1
    return {"cases": len(traces), "paths": dict(counts), "is_accuracy": False}


def run(dataset, cases, root, gate, provider, client=None):
    policy = CONFIG.policy()
    reader = (OfflineReader(policy) if client is None else CalculatingReader(client,
        ReaderSpec("deepseek", MODEL, prompt_version=VERSION, max_tokens=OUTPUT_CAP), policy=policy))
    if client:
        client.phase = "reader-" + root.name

    def progress(event):
        print(json.dumps({"stage": root.name, **event}), flush=True)
        if client and event.get("status") == "DEGRADED":
            trace = read_json(root / "traces" / trace_filename(event["question_id"]))
            if trace.get("reader", {}).get("status") in {"ERROR", "TIMEOUT"}:
                raise PilotStopped("Reader transport/budget failure; no batch fallback.")

    runner = OracleRunner(dataset, root, reader, gate=gate, gate_name="semantic-utility-v2.1",
        embedder=provider, embedder_name=f"{DEFAULT_MODEL}@{DEFAULT_REVISION};max_length=512",
        pipeline_config=CONFIG, selection=cases, selection_meta={"ids": [c.question_id for c in cases]},
        progress=progress)
    runner.run(resume=(root / "manifest.json").exists())
    audit = audit_run_context(root)
    atomic_write_json(root / "context-audit.json", audit)
    traces = _load_run(root)[1]
    if (audit["auditable_cases"] != len(cases)
            or audit["reader_context_heuristic_tokens"]["over_budget_cases"]
            or any(t["status"] == "FAILED" for t in traces.values())):
        raise PilotStopped("Incomplete or over-budget evidence; stop before judging.")
    return traces


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paid", action="store_true")
    ap.add_argument("--cache-only", action="store_true", help="Replay exact cached requests; zero new API calls")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--api-key-env", default="DIVE_READER_API_KEY")
    ap.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-calculation-20260930")
    args = ap.parse_args()
    if args.full and not args.paid:
        ap.error("--full requires --paid")
    if args.paid and args.cache_only:
        ap.error("--paid and --cache-only are mutually exclusive")
    root = args.output.resolve()
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    ids = read_json(ROOT / "eval/reports/oracle-reasoning-20260929/frozen-plan.json")["ids"]
    cases = [dataset.by_id()[qid] for qid in ids]
    if len(cases) != 80 or sum(c.should_abstain for c in cases) != 30:
        raise PilotStopped("Screen must contain 50 answerable and all 30 unanswerable cases.")
    ledgers = budget_ledgers(root)
    caches = [ROOT / "eval/reports" / n / "api-cache" for n in PRIOR_RUNS]
    caches.extend(p.parent for p in ledgers if p.parent != root / "api-cache")
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT), "ids": ids,
        "selection_uses_correctness": False, "config": CONFIG.to_dict(),
        "reader_prompt_version": VERSION, "reader_max_tokens": OUTPUT_CAP, "fallback_max_tokens": 512,
        "reader_extra_body": EXTRA, "judge_extra_body": EXTRA,
        "execution_mode": "cache_only" if args.cache_only else "paid_or_offline",
        "screen_status": "Reused public development screen; not a blind holdout",
        "offline_gate": "All formation, retrieval and reader-visible evidence equal to the frozen control",
        "paid_gate": "+3/50 answerable, no loss on all 30 unanswerable; no full run otherwise",
        "aggregate_budget": read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json"),
        "currency_ledger_paths": [str(p) for p in ledgers],
        "caveat": "Same-family non-official judge. Calculator verifies source literals, not semantic completeness."}
    target = root / "frozen-plan.json"
    if target.exists() and read_json(target) != plan:
        raise PilotStopped("Frozen calculation experiment changed; use a new directory.")
    atomic_write_json(target, plan)
    old = _load_run(PREVIOUS / "full-wide-indexed")[1]
    _, old_v = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id: old[c.question_id] for c in cases}
    baseline = summarize(cases, before, old_v)
    atomic_write_json(root / "baseline-evaluation.json", baseline)
    atomic_write_json(root / "route-summary.json", route_summary(cases))
    atomic_write_json(root / "aggregate-peak-cost-before.json", peak_cost(ledgers))
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    offline = run(dataset, cases, root / "offline-candidate", gate, provider)
    atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before, offline))
    if not args.paid and not args.cache_only:
        print(json.dumps({"offline_complete": True, "routed": route_summary(cases)["routed_cases"]}), flush=True)
        return
    if args.cache_only:
        guard = None
        # max_calls=0 stops a miss BEFORE reservation or transport. This client
        # never possesses credentials, and cached request identities stay exact.
        client = MeteredClient(ScriptedChatClient(), root / "api-cache", max_calls=0,
            max_tokens=2500000, reuse_roots=caches)
    else:
        key = os.environ.get(args.api_key_env) or getpass.getpass("DeepSeek key (hidden, never saved): ")
        if not key.strip():
            raise PilotStopped("Missing credential; no paid request sent.")
        if MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
            raise PilotStopped("Requested model unavailable.")
        # A local threshold never renews the shared planning budget.
        guard = TotalBudgetGuard(key, root, limit=Decimal("2.00"))
        guard.check(0, force=True)
        client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA),
            root / "api-cache", max_calls=1000, max_tokens=2500000, reuse_roots=caches,
            before_request=guard.check, currency_guard=CurrencyBudget(ledgers))
    try:
        screen_root = root / "screen-calculation"
        traces = run(dataset, cases, screen_root, gate, provider, client)
        atomic_write_json(root / "screen-pair-audit.json", evidence_audit(before, traces))
        verdicts = judge_run(client, dataset, cases, screen_root, root / "screen-verdicts.jsonl")
        report = summarize(cases, traces, verdicts)
        atomic_write_json(root / "screen-evaluation.json", report)
        atomic_write_json(root / "screen-computations.json", computation_summary(traces))
        atomic_write_json(root / "screen-comparison.json", _accuracy(before, traces, old_v, verdicts,
            {c.question_id: c.question_type for c in cases}))
        selected = eligible(baseline, report)
        atomic_write_json(root / "selection.json", {"eligible_for_full": selected})
        print(json.dumps({"baseline": baseline["overall"], "candidate": report["overall"],
                          "eligible_for_full": selected}), flush=True)
        if selected and args.full:
            out = root / "full-calculation"
            full = run(dataset, dataset.cases, out, gate, provider, client)
            atomic_write_json(root / "full-pair-audit.json", evidence_audit(old, full))
            validation = validate_submission(out / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
            atomic_write_json(root / "submission-validation.json", validation.to_dict())
            if validation.status != "VALID":
                raise PilotStopped("Invalid full submission.")
            full_v = judge_run(client, dataset, dataset.cases, out, root / "full-verdicts.jsonl")
            atomic_write_json(root / "full-evaluation.json", summarize(dataset.cases, full, full_v, exposed_ids=ids))
            atomic_write_json(root / "full-comparison.json", _accuracy(old, full, old_v, full_v,
                {c.question_id: c.question_type for c in dataset.cases}))
            atomic_write_json(root / "full-computations.json", computation_summary(full))
        if guard:
            guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"screen_complete": True,
            "full_run": "COMPLETED" if selected and args.full else "NOT_RUN", "official_score": "NOT_EVALUATED"})
    except PilotStopped as exc:
        atomic_write_json(root / "stopped.json", {"status": "STOPPED", "reason": str(exc), "is_accuracy": False})
        raise
    finally:
        atomic_write_json(root / "usage.json", client.summary())
        atomic_write_json(root / "aggregate-peak-cost.json", peak_cost(ledgers))


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
