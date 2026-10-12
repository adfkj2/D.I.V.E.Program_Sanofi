"""Same low thinking with a larger final-output allowance and shared spend cap."""
from __future__ import annotations

import argparse
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
from oracle_narrative_experiment import TotalBudgetGuard
from oracle_structural_experiment import CachedEmbedder
from oracle_complete_test import judge_run, summarize, read_json
from oracle_reader_pair import _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_v4_experiment import OfflineReader, evidence_audit
from oracle_reasoning_experiment import CONFIG, THINKING
from oracle_calculation_experiment import budget_ledgers, eligible
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.reader import ReaderSpec
from dive_memory.oracle.indexed_reader import IndexedEvidenceReader
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, trace_filename
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.eval_manifest import code_snapshot

PREVIOUS = ROOT / "eval/reports/oracle-narrative-20260928-v2"
OUTPUT_CAP = 8192


def run(dataset, cases, root, gate, provider, client=None, *, reader=None):
    policy = CONFIG.policy()
    if reader is None:
        reader = (OfflineReader(policy) if client is None else IndexedEvidenceReader(client,
            ReaderSpec("deepseek", MODEL, prompt_version=policy.prompt_version,
                       max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy))
    if client:
        client.phase = "reader-" + root.name

    def progress(event):
        print(json.dumps({"stage": root.name, **event}), flush=True)
        if client and event.get("status") == "DEGRADED":
            trace = read_json(root / "traces" / trace_filename(event["question_id"]))
            if trace.get("reader", {}).get("status") in {"ERROR", "TIMEOUT"}:
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
        raise PilotStopped("Incomplete or over-budget run.")
    return traces


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paid", action="store_true")
    ap.add_argument("--api-key-env", default="DIVE_READER_API_KEY")
    ap.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-extended-reasoning-20260930")
    args = ap.parse_args()
    root = args.output.resolve()
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    ids = read_json(ROOT / "eval/reports/oracle-reasoning-20260929/frozen-plan.json")["ids"]
    cases = [dataset.by_id()[qid] for qid in ids]
    ledgers = set(budget_ledgers(root))
    ledgers.update((ROOT / "eval/reports").glob("oracle-extended-reasoning-*/api-cache/ledger.json"))
    ledgers = sorted(ledgers)
    caches = [p.parent for p in ledgers if p.parent != root / "api-cache"]
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT), "ids": ids,
        "selection_uses_correctness": False, "config": CONFIG.to_dict(), "reader_max_tokens": OUTPUT_CAP,
        "reader_extra_body": THINKING, "judge_extra_body": EXTRA,
        "selection": "+3/50 answerable, no loss on 30 unanswerable",
        "aggregate_budget": read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json"),
        "currency_ledger_paths": [str(p) for p in ledgers],
        "caveat": "Same-family non-official judge, reused development screen, larger output cap includes reasoning.",
        "full_run_policy": "Screen only under the current remaining budget; never start an unaffordable full run."}
    target = root / "frozen-plan.json"
    if target.exists() and read_json(target) != plan:
        raise PilotStopped("Frozen extended-thinking experiment changed; use a new directory.")
    atomic_write_json(target, plan)
    old = _load_run(PREVIOUS / "full-wide-indexed")[1]
    _, old_v = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id: old[c.question_id] for c in cases}
    baseline = summarize(cases, before, old_v)
    atomic_write_json(root / "baseline-evaluation.json", baseline)
    atomic_write_json(root / "aggregate-peak-cost-before.json", peak_cost(ledgers))
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    offline = run(dataset, cases, root / "offline-baseline", gate, provider)
    atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before, offline))
    if not args.paid:
        return
    key = os.environ.get(args.api_key_env) or getpass.getpass("DeepSeek key (hidden, never saved): ")
    if not key.strip():
        raise PilotStopped("Missing credential.")
    if MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
        raise PilotStopped("Requested model unavailable.")
    guard = TotalBudgetGuard(key, root, limit=Decimal("1.80"))
    guard.check(0, force=True)
    normal = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA)
    thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=THINKING)
    client = MeteredClient(thinking, root / "api-cache", max_calls=180, max_tokens=2500000,
        reuse_roots=caches, before_request=guard.check, currency_guard=CurrencyBudget(ledgers))
    try:
        out = root / "screen-low-thinking-8192"
        traces = run(dataset, cases, out, gate, provider, client)
        atomic_write_json(root / "screen-pair-audit.json", evidence_audit(before, traces))
        client.client = normal
        verdicts = judge_run(client, dataset, cases, out, root / "screen-verdicts.jsonl")
        report = summarize(cases, traces, verdicts)
        atomic_write_json(root / "screen-evaluation.json", report)
        atomic_write_json(root / "screen-comparison.json", _accuracy(before, traces, old_v, verdicts,
            {c.question_id: c.question_type for c in cases}))
        atomic_write_json(root / "selection.json", {"eligible_for_full": eligible(baseline, report),
            "full_run": "NOT_RUN", "reason": "Current budget reserved for screening; no budget renewal"})
        guard.check(client.summary()["attempts"], force=True)
        snapshot = code_snapshot(ROOT)
        if snapshot != plan["source_snapshot"]:
            raise PilotStopped("Source or dependency drift during the run.")
        atomic_write_json(root / "end-source-audit.json", {"source_and_dependencies_unchanged": True,
            "sha256": snapshot["sha256"]})
        atomic_write_json(root / "completion.json", {"screen_complete": True, "full_run": "NOT_RUN",
            "official_score": "NOT_EVALUATED"})
        print(json.dumps({"baseline": baseline["overall"], "candidate": report["overall"],
                          "eligible_for_full": eligible(baseline, report)}), flush=True)
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
