"""Paired clause/document repairs: free evidence checks before bounded answer tests."""
from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
import getpass
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from oracle_low_cost_pilot import MeteredClient, PilotStopped, private_get, MODEL, ENDPOINT, EXTRA
from oracle_currency_budget import CurrencyBudget, peak_cost
from oracle_narrative_experiment import ARMS as PREVIOUS_ARMS, TotalBudgetGuard, run
from oracle_structural_experiment import CachedEmbedder, cov_metrics
from oracle_complete_test import judge_run, summarize, read_json
from oracle_reader_pair import _load_run, _load_verdicts, _accuracy
from oracle_v4_experiment import coverage, evidence_audit
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.runner import atomic_write_json
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.validator import validate_submission
from dive_memory.eval_manifest import code_snapshot

PREVIOUS = ROOT / "eval/reports/oracle-narrative-20260928-v2"
BASE = replace(PREVIOUS_ARMS["wide"], reader_policy_name="indexed_evidence")
DOCUMENTS = dict(controlled_document_limit=4, controlled_document_token_budget=2400,
                 controlled_document_char_budget=2400, expand_document_spans=True)
ARMS = {"baseline": BASE, "clauses": replace(BASE, narrative_clause_anchors=True),
        "documents": replace(BASE, **DOCUMENTS),
        "combined": replace(BASE, narrative_clause_anchors=True, **DOCUMENTS)}


def visible_chars(traces):
    return sum(len(t["reader_context_text"]) for t in traces.values())


def select(metrics):
    baseline = metrics["baseline"]
    candidates = []
    for name, m in metrics.items():
        if name == "baseline":
            continue
        if (m["packed"] < baseline["packed"] or m["complete_positive_cases"] < baseline["complete_positive_cases"]
                or any(m["by_type_packed"].get(t, 0) < n for t, n in baseline["by_type_packed"].items())):
            continue
        # Source recall alone cannot detect retaining a missing list-item head.
        # More visible text is only an eligibility signal, NEVER an accuracy.
        if m["packed"] > baseline["packed"] or m["visible_chars"] > baseline["visible_chars"] * 1.05:
            candidates.append(name)
    return max(candidates, key=lambda n: (metrics[n]["complete_positive_cases"], metrics[n]["packed"],
        metrics[n]["visible_chars"], -list(ARMS).index(n)), default=None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paid", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-context-repair-20260929")
    args = ap.parse_args()
    if args.full and not args.paid:
        ap.error("--full requires --paid")
    root = args.output
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    ids = read_json(ROOT / "eval/reports/oracle-reasoning-20260929/frozen-plan.json")["ids"]
    cases = [dataset.by_id()[qid] for qid in ids]
    names = ("oracle-narrative-20260928", "oracle-narrative-20260928-v2", "oracle-reasoning-20260929")
    caches = [ROOT / "eval/reports" / n / "api-cache" for n in names]
    ledgers = [p / "ledger.json" for p in [*caches, root / "api-cache"]]
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT), "ids": ids,
        "arms": {n:c.to_dict() for n,c in ARMS.items()}, "selection_uses_correctness": False,
        "screen_status": "Same public development screen used for thinking ablation; not a blind holdout",
        "offline_gate": "nondecreasing coverage in every type; added coverage or >5% more visible chars",
        "paid_gate": "+3/50 answerable, no loss on all 30 unanswerable",
        "reader_extra_body": EXTRA, "judge_extra_body": EXTRA,
        "currency_ledger_paths": [str(p.relative_to(ROOT)) for p in ledgers],
        "aggregate_budget": read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json")}
    target = root / "frozen-plan.json"
    if target.exists() and read_json(target) != plan:
        raise PilotStopped("Frozen context repair experiment changed; use a new directory.")
    atomic_write_json(target, plan)
    old = _load_run(PREVIOUS / "full-wide-indexed")[1]
    _, old_v = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id:old[c.question_id] for c in cases}
    baseline = summarize(cases, before, old_v)
    atomic_write_json(root / "baseline-evaluation.json", baseline)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    metrics = {}
    for name, config in ARMS.items():
        traces = run(dataset, cases, config, root / ("offline-"+name), gate, provider)
        if name == "baseline":
            atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before,traces))
        cov = coverage(cases,traces)
        atomic_write_json(root / (name+"-coverage.json"), cov)
        metrics[name] = {**cov_metrics(cov), "visible_chars":visible_chars(traces)}
    selected = select(metrics)
    atomic_write_json(root / "offline-summary.json", {"metrics":metrics, "selected":selected,
        "is_accuracy":False})
    print(json.dumps({"offline_complete":True, "selected":selected, "metrics":metrics}),flush=True)
    if not args.paid or not selected:
        return
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if MODEL not in [r["id"] for r in private_get("models",key).get("data",[])]:
        raise PilotStopped("Model unavailable.")
    guard = TotalBudgetGuard(key,root,limit=Decimal("2.50"))
    client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT,key,provider="deepseek",extra_body=EXTRA),
        root / "api-cache",max_calls=1200,max_tokens=3500000,reuse_roots=caches,
        before_request=guard.check,currency_guard=CurrencyBudget(ledgers))
    try:
        out = root / ("screen-"+selected)
        traces = run(dataset,cases,ARMS[selected],out,gate,provider,client)
        atomic_write_json(root / "screen-pair-audit.json", evidence_audit(
            _load_run(root / ("offline-"+selected))[1],traces))
        verdicts = judge_run(client,dataset,cases,out,root / "screen-verdicts.jsonl")
        report = summarize(cases,traces,verdicts)
        atomic_write_json(root / "screen-evaluation.json",report)
        eligible = (report["answerable"]["correct"] >= baseline["answerable"]["correct"] + 3
                    and report["unanswerable"]["correct"] >= baseline["unanswerable"]["correct"])
        atomic_write_json(root / "selection.json",{"selected":selected,"eligible_for_full":eligible})
        print(json.dumps({"candidate":report["overall"],"eligible_for_full":eligible}),flush=True)
        if eligible and args.full:
            full_root = root / ("full-"+selected)
            full = run(dataset,dataset.cases,ARMS[selected],full_root,gate,provider,client)
            validation = validate_submission(full_root / "submission.jsonl",expected_ids=dataset.question_ids,require_full=True)
            atomic_write_json(root / "submission-validation.json",validation.to_dict())
            if validation.status != "VALID":
                raise PilotStopped("Invalid full submission.")
            full_v = judge_run(client,dataset,dataset.cases,full_root,root / "full-verdicts.jsonl")
            atomic_write_json(root / "full-evaluation.json",summarize(dataset.cases,full,full_v,exposed_ids=ids))
            atomic_write_json(root / "full-comparison.json",_accuracy(old,full,old_v,full_v,
                {c.question_id:c.question_type for c in dataset.cases}))
            atomic_write_json(root / "full-coverage.json",coverage(dataset.cases,full))
        guard.check(client.summary()["attempts"],force=True)
        atomic_write_json(root / "completion.json",{"screen_complete":True,
            "full_run":"COMPLETED" if eligible and args.full else "NOT_RUN","official_score":"NOT_EVALUATED"})
    finally:
        atomic_write_json(root / "usage.json",client.summary())
        atomic_write_json(root / "aggregate-peak-cost.json",peak_cost(ledgers))


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc),flush=True)
        sys.exit(2)
