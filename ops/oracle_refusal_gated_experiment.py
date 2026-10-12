"""Zero-new-call evaluation of a uniform refusal gate, replaying exact requests."""
import argparse
import json
import os
import getpass
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from oracle_extended_reasoning_experiment import run, CONFIG, THINKING, OUTPUT_CAP, PREVIOUS
from oracle_calculation_experiment import budget_ledgers, eligible
from oracle_currency_budget import peak_cost, CurrencyBudget
from oracle_low_cost_pilot import MeteredClient, PilotStopped, ENDPOINT, EXTRA, MODEL, private_get
from oracle_narrative_experiment import TotalBudgetGuard
from oracle_reader_pair import _load_run, _load_verdicts, _accuracy
from oracle_complete_test import read_json, judge_run, summarize
from oracle_structural_experiment import CachedEmbedder
from oracle_retrieval_ablation import model_provider, make_gate, DEFAULT_MODEL, DEFAULT_REVISION
from oracle_v4_experiment import evidence_audit
from dive_memory.oracle.refusal_gated_reader import RefusalGatedReader, VERSION
from dive_memory.oracle.adjudicated_reader import AdjudicatedReader, EvidenceReferee, REFEREE_VERSION, VERSION as REVIEW_VERSION
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.indexed_reader import IndexedEvidenceReader
from dive_memory.oracle.reader import ReaderSpec
from dive_memory.oracle.runner import atomic_write_json
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.eval_manifest import code_snapshot


class CacheTransport:
    endpoint = ENDPOINT

    def __init__(self, extra_body):
        self.extra_body = extra_body

    def complete(self, **kwargs):
        raise AssertionError("Cache-only transport must never run")


class ModeClient:
    def __init__(self, meter, transport):
        self.meter, self.transport = meter, transport

    def complete(self, **kwargs):
        # Sequential readers share one durable meter; no competing ledger state.
        self.meter.client = self.transport
        return self.meter.complete(**kwargs)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adjudicate", action="store_true")
    ap.add_argument("--paid", action="store_true", help="Bounded new evidence-review calls; only with --adjudicate")
    ap.add_argument("--api-key-env", default="DIVE_READER_API_KEY")
    ap.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-refusal-gated-thinking-20261004")
    args = ap.parse_args()
    if args.paid and not args.adjudicate:
        ap.error("Paid execution requires --adjudicate; the rigid gate cannot reach the 90 percent target")
    root = args.output.resolve()
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    ids = read_json(ROOT / "eval/reports/oracle-reasoning-20260929/frozen-plan.json")["ids"]
    cases = [dataset.by_id()[qid] for qid in ids]
    ledgers = budget_ledgers(root)
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT), "ids": ids,
        "config": CONFIG.to_dict(), "reader_version": REVIEW_VERSION if args.adjudicate else VERSION,
        "execution_mode": "bounded_paid" if args.paid else "cache_only",
        "routing": "Independent evidence review on answerability disagreement" if args.adjudicate else
                   "Preserve every primary non-ANSWER; otherwise use secondary, including its refusal or error",
        "primary_extra_body": EXTRA, "secondary_extra_body": THINKING,
        "primary_max_tokens": 512, "secondary_max_tokens": OUTPUT_CAP,
        "selection_uses_correctness": False, "selection": "+3/50 answerable, no loss on all 30 unanswerable",
        "aggregate_budget": read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json"),
        "currency_ledger_paths": [str(p) for p in ledgers],
        "caveat": "Public reused development screen; uniform runtime policy, not per-ID answer selection; non-official judge."}
    path = root / "frozen-plan.json"
    if path.exists() and read_json(path) != plan:
        raise PilotStopped("Frozen refusal-gate experiment changed; use a new directory.")
    atomic_write_json(path, plan)
    normal, thinking = CacheTransport(EXTRA), CacheTransport(THINKING)
    guard = None
    if args.paid:
        key = os.environ.get(args.api_key_env) or getpass.getpass("DeepSeek key (hidden, never saved): ")
        if not key.strip() or MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
            raise PilotStopped("Missing credential or unavailable model.")
        guard = TotalBudgetGuard(key, root, limit=Decimal("0.70"))
        guard.check(0, force=True)
        normal = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA)
        thinking = OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=THINKING)
    client = MeteredClient(normal, root / "api-cache", max_calls=50 if args.paid else 0, max_tokens=2500000,
        reuse_roots=[p.parent for p in ledgers if p.parent != root / "api-cache"],
        before_request=guard.check if guard else None, currency_guard=CurrencyBudget(ledgers) if args.paid else None)
    policy = CONFIG.policy()
    primary = IndexedEvidenceReader(ModeClient(client, normal), ReaderSpec("deepseek", MODEL,
        prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    secondary = IndexedEvidenceReader(ModeClient(client, thinking), ReaderSpec("deepseek", MODEL,
        prompt_version=policy.prompt_version, max_tokens=OUTPUT_CAP, timeout_seconds=120), policy=policy)
    referee = EvidenceReferee(ModeClient(client, normal), ReaderSpec("deepseek", MODEL,
        prompt_version=REFEREE_VERSION, max_tokens=1024), policy=policy)
    reader = AdjudicatedReader(primary, secondary, referee) if args.adjudicate else RefusalGatedReader(primary, secondary)
    old = _load_run(PREVIOUS / "full-wide-indexed")[1]
    _, old_v = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id: old[c.question_id] for c in cases}
    baseline = summarize(cases, before, old_v)
    atomic_write_json(root / "baseline-evaluation.json", baseline)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    try:
        if args.paid:
            offline = run(dataset, cases, root / "offline-before-review", gate, provider)
            atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before, offline))
        out = root / "screen-refusal-gated"
        traces = run(dataset, cases, out, gate, provider, client, reader=reader)
        atomic_write_json(root / "screen-pair-audit.json", evidence_audit(before, traces))
        client.client = normal
        verdicts = judge_run(client, dataset, cases, out, root / "screen-verdicts.jsonl")
        report = summarize(cases, traces, verdicts)
        atomic_write_json(root / "screen-evaluation.json", report)
        atomic_write_json(root / "screen-comparison.json", _accuracy(before, traces, old_v, verdicts,
            {c.question_id: c.question_type for c in cases}))
        atomic_write_json(root / "selection.json", {"eligible_for_full": eligible(baseline, report)})
        if code_snapshot(ROOT) != plan["source_snapshot"]:
            raise PilotStopped("Source or dependency drift during replay.")
        if guard:
            guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"screen_complete": True, "new_api_calls": client.summary()["attempts"],
            "full_run": "NOT_RUN", "official_score": "NOT_EVALUATED"})
        print(json.dumps({"baseline": baseline["overall"], "candidate": report["overall"],
            "eligible_for_full": eligible(baseline, report)}), flush=True)
    finally:
        atomic_write_json(root / "usage.json", client.summary())
        atomic_write_json(root / "aggregate-peak-cost.json", peak_cost(ledgers))


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
