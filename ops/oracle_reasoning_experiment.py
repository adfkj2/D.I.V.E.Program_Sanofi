"""Fixed-evidence low-thinking ablation. Offline by default, bounded paid opt-in."""
from __future__ import annotations

import argparse
from decimal import Decimal
import getpass
from pathlib import Path
import sys
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from oracle_low_cost_pilot import MeteredClient, PilotStopped, private_get, MODEL, ENDPOINT, EXTRA
from oracle_currency_budget import CurrencyBudget, peak_cost
from oracle_narrative_experiment import ARMS, TotalBudgetGuard
from oracle_structural_experiment import CachedEmbedder
from oracle_complete_test import judge_run, summarize, read_json
from oracle_reader_pair import plan_selection, _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_v4_experiment import OfflineReader, evidence_audit
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.reader import ReaderSpec
from dive_memory.oracle.indexed_reader import IndexedEvidenceReader
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, trace_filename
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.validator import validate_submission
from dive_memory.eval_manifest import code_snapshot
from dataclasses import replace

PREVIOUS = ROOT / "eval/reports/oracle-narrative-20260928-v2"
CONFIG = replace(ARMS["wide"], reader_policy_name="indexed_evidence")
THINKING = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
OUTPUT_CAP = 2048  # Includes billed reasoning tokens, not just visible JSON.


def eligible(baseline, candidate):
    return (candidate["answerable"]["correct"] >= baseline["answerable"]["correct"] + 3
            and candidate["unanswerable"]["correct"] >= baseline["unanswerable"]["correct"])


def run(dataset, cases, root, gate, provider, client=None):
    policy = CONFIG.policy()
    reader = (OfflineReader(policy) if client is None else IndexedEvidenceReader(client,
        ReaderSpec("deepseek", MODEL, prompt_version=policy.prompt_version,
                   max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy))
    if client:
        client.phase = "reader-" + root.name
    def progress(event):
        print(json.dumps({"stage": root.name, **event}), flush=True)
        if client and event.get("status") == "DEGRADED":
            t = read_json(root / "traces" / trace_filename(event["question_id"]))
            if t.get("reader", {}).get("status") in {"ERROR", "TIMEOUT"}:
                raise PilotStopped("Reader transport failure; stop without batch fallback.")
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
        raise PilotStopped("Incomplete or over-budget run; do not continue paid evaluation.")
    return traces


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paid", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-reasoning-20260929")
    args = ap.parse_args()
    if args.full and not args.paid:
        ap.error("--full requires --paid")
    root = args.output
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    exposed = set()
    for name in ("oracle-structural-20260928-v2", "oracle-v4-improvement-20260927",
                 "oracle-narrative-20260928-v2"):
        exposed.update(read_json(ROOT / "eval/reports" / name / "frozen-plan.json")["ids"])
    cases = plan_selection([c for c in dataset.cases if c.should_abstain or c.question_id not in exposed],
                           size=80, abstention=30, seed=20260929)
    prior_roots = [ROOT / "eval/reports" / n / "api-cache" for n in
                   ("oracle-narrative-20260928", "oracle-narrative-20260928-v2")]
    ledgers = [p / "ledger.json" for p in [*prior_roots, root / "api-cache"]]
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT),
        "ids": [c.question_id for c in cases], "seed": 20260929, "selection_uses_correctness": False,
        "config": CONFIG.to_dict(), "reader_extra_body": THINKING, "judge_extra_body": EXTRA,
        "reader_max_tokens": OUTPUT_CAP, "temperature_ignored_in_thinking": True,
        "selection": "+3/50 answerable, no loss on 30 unanswerable; otherwise no full run",
        "aggregate_budget": read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json"),
        "currency_ledger_paths": [str(p.relative_to(ROOT)) for p in ledgers],
        "peak_input_cny_per_million": 2, "peak_output_cny_per_million": 8,
        "caveat": "Same-family non-official judge; public development set, not a blind holdout."}
    target = root / "frozen-plan.json"
    if target.exists() and read_json(target) != plan:
        raise PilotStopped("Frozen reasoning experiment changed; use a new output directory.")
    atomic_write_json(target, plan)
    old = _load_run(PREVIOUS / "full-wide-indexed")[1]
    _, old_v = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id: old[c.question_id] for c in cases}
    baseline = summarize(cases, before, old_v)
    atomic_write_json(root / "baseline-evaluation.json", baseline)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    offline = run(dataset, cases, root / "offline-baseline", gate, provider)
    atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before, offline))
    if not args.paid:
        return
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
        raise PilotStopped("Requested model unavailable.")
    guard = TotalBudgetGuard(key, root, limit=Decimal("3.00"))
    normal = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA)
    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=THINKING)
    client = MeteredClient(thinking, root / "api-cache", max_calls=1200, max_tokens=3500000,
        reuse_roots=prior_roots, before_request=guard.check, currency_guard=CurrencyBudget(ledgers))
    try:
        screen_root = root / "screen-low-thinking"
        traces = run(dataset, cases, screen_root, gate, provider, client)
        atomic_write_json(root / "screen-pair-audit.json", evidence_audit(before, traces))
        client.client = normal  # Judge remains the original non-thinking model.
        verdicts = judge_run(client, dataset, cases, screen_root, root / "screen-verdicts.jsonl")
        report = summarize(cases, traces, verdicts)
        atomic_write_json(root / "screen-evaluation.json", report)
        chosen = eligible(baseline, report)
        atomic_write_json(root / "selection.json", {"eligible_for_full": chosen})
        print(json.dumps({"baseline": baseline["overall"], "candidate": report["overall"],
                          "eligible_for_full": chosen}), flush=True)
        if chosen and args.full:
            client.client = thinking
            full_root = root / "full-low-thinking"
            full = run(dataset, dataset.cases, full_root, gate, provider, client)
            atomic_write_json(root / "full-pair-audit.json", evidence_audit(old, full))
            validation = validate_submission(full_root / "submission.jsonl",
                                             expected_ids=dataset.question_ids, require_full=True)
            atomic_write_json(root / "submission-validation.json", validation.to_dict())
            if validation.status != "VALID":
                raise PilotStopped("Invalid full submission.")
            client.client = normal
            full_v = judge_run(client, dataset, dataset.cases, full_root, root / "full-verdicts.jsonl")
            atomic_write_json(root / "full-evaluation.json",
                summarize(dataset.cases, full, full_v, exposed_ids=plan["ids"]))
            atomic_write_json(root / "full-comparison.json", _accuracy(old, full, old_v, full_v,
                {c.question_id: c.question_type for c in dataset.cases}))
        guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"screen_complete": True,
            "full_run": "COMPLETED" if chosen and args.full else "NOT_RUN",
            "official_score": "NOT_EVALUATED"})
    finally:
        atomic_write_json(root / "usage.json", client.summary())
        atomic_write_json(root / "aggregate-peak-cost.json", peak_cost(ledgers))


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
