"""Frozen source-window and indexed-reader ablation. Offline by default."""
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
from oracle_structural_experiment import CachedEmbedder, cov_metrics
from oracle_low_cost_pilot import MeteredClient, PilotStopped, private_get, MODEL, ENDPOINT, EXTRA
from oracle_complete_test import BalanceGuard, judge_run, summarize, read_json
from oracle_reader_pair import plan_selection, _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_v4_experiment import OfflineReader, coverage, evidence_audit
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.reader import ReaderSpec, GroundedChatReader
from dive_memory.oracle.indexed_reader import IndexedEvidenceReader
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, trace_filename
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.validator import validate_submission
from dive_memory.eval_manifest import code_snapshot

PREVIOUS = ROOT / "eval/reports/oracle-structural-20260928-v2"
BASE = PipelineConfig(scoring_policy_name="balanced", document_selection_name="query_excerpt",
    document_chunk_search_name="controlled_supplement", episodic_retention=True, semantic_retrieval_only=True)
ARMS = {"baseline": BASE,
        "narrative": replace(BASE, narrative_retention=True),
        "diverse": replace(BASE, narrative_retention=True, source_diversity=True),
        "wide": replace(BASE, narrative_retention=True, source_diversity=True, limit=30, token_budget=6000)}


class TotalBudgetGuard(BalanceGuard):
    def check(self, attempts, *, force=False):
        super().check(attempts, force=force)
        rules = read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json")
        balance = read_json(self.root / "balance-latest.json")
        spent = Decimal(rules["initial_observed_account_balance_cny"]) - Decimal(balance["remaining_cny"])
        if spent >= Decimal(rules["planning_stop_cny"]):
            raise PilotStopped("Aggregate budget reached; retain settlement reserve, stop all paid work.")


def run(dataset, cases, config, root, gate, provider, client=None):
    policy = config.policy()
    if client is None:
        reader = OfflineReader(policy)
    else:
        client.phase = "reader-" + root.name
        cls = IndexedEvidenceReader if policy.name == "indexed_evidence" else GroundedChatReader
        reader = cls(client, ReaderSpec("deepseek", MODEL, prompt_version=policy.prompt_version,
                                      max_tokens=512), policy=policy)
    def progress(event):
        print(json.dumps({"stage": root.name, **event}), flush=True)
        if client and event.get("status") == "DEGRADED":
            trace = read_json(root / "traces" / trace_filename(event["question_id"]))
            if trace.get("reader", {}).get("status") in {"ERROR", "TIMEOUT"}:
                raise PilotStopped("Reader transport/budget failure; stop before processing further cases.")
    runner = OracleRunner(dataset, root, reader, gate=gate, gate_name="semantic-utility-v2.1",
        embedder=provider, embedder_name=f"{DEFAULT_MODEL}@{DEFAULT_REVISION};max_length=512",
        pipeline_config=config, selection=cases, selection_meta={"ids": [c.question_id for c in cases]},
        progress=progress)
    runner.run(resume=(root / "manifest.json").exists())
    traces = _load_run(root)[1]
    audit = audit_run_context(root)
    atomic_write_json(root / "context-audit.json", audit)
    if audit["auditable_cases"] != len(cases) or audit["reader_context_heuristic_tokens"]["over_budget_cases"]:
        raise PilotStopped("Incomplete or over-budget evidence.")
    if any(t["status"] == "FAILED" for t in traces.values()):
        raise PilotStopped("Pipeline failure; stop before judging.")
    return traces


def select_writer(metrics):
    b = metrics["baseline"]
    eligible = [n for n in ARMS if n != "baseline" and metrics[n]["packed"] >= b["packed"]
        and metrics[n]["complete_positive_cases"] >= b["complete_positive_cases"]
        and all(metrics[n]["by_type_packed"].get(t, 0) >= v - 2 for t, v in b["by_type_packed"].items())]
    return max(eligible, key=lambda n: (metrics[n]["complete_positive_cases"], metrics[n]["packed"],
                                      -list(ARMS).index(n)), default=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-narrative-20260928-v2")
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--full", action="store_true", help="one eligible full run after paid screening")
    args = parser.parse_args()
    if args.full and not args.paid:
        parser.error("--full requires --paid")
    root = args.output
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    exposed = set(read_json(PREVIOUS / "frozen-plan.json")["ids"])
    exposed.update(read_json(ROOT / "eval/reports/oracle-v4-improvement-20260927/frozen-plan.json")["ids"])
    pool = [c for c in dataset.cases if c.should_abstain or c.question_id not in exposed]
    cases = plan_selection(pool, size=80, abstention=30, seed=20260930)
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT),
        "ids": [c.question_id for c in cases], "arms": {n: c.to_dict() for n,c in ARMS.items()},
        "selection_uses_correctness": False, "writer_selection": "nondecreasing packed and complete cases; type loss <=2; best complete/packed/simpler",
        "paid_arms": "indexed baseline plus best writer with v3 and indexed reader",
        "paid_selection": "answerable +3/50; unanswerable not lower; weighted ranking; simpler ties",
        "max_calls": 1500, "max_tokens": 3500000, "balance_stop_cny": "4.00",
        "aggregate_budget": read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json")}
    target = root / "frozen-plan.json"
    if target.exists() and read_json(target) != plan:
        raise PilotStopped("Frozen configuration/source changed; choose a new output directory.")
    atomic_write_json(target, plan)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    old = _load_run(PREVIOUS / "full-episodes-semantic")[1]
    _, old_v = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id: old[c.question_id] for c in cases}
    metrics = {}
    for arm, config in ARMS.items():
        traces = run(dataset, cases, config, root / ("offline-"+arm), gate, provider)
        if arm == "baseline":
            atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before, traces))
        cov = coverage(cases, traces)
        atomic_write_json(root / (arm+"-coverage.json"), cov)
        metrics[arm] = cov_metrics(cov)
    writer = select_writer(metrics)
    atomic_write_json(root / "offline-summary.json", {"metrics": metrics, "selected_writer": writer,
                                                       "score_type": "COVERAGE_NOT_ACCURACY"})
    print(json.dumps({"offline_complete": True, "writer": writer, "metrics": metrics}), flush=True)
    if not args.paid:
        return
    configs = {"indexed-baseline": replace(BASE, reader_policy_name="indexed_evidence")}
    if writer:
        configs[writer+"-v3"] = ARMS[writer]
        configs[writer+"-indexed"] = replace(ARMS[writer], reader_policy_name="indexed_evidence")
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
        raise PilotStopped("Model unavailable")
    guard = TotalBudgetGuard(key, root, limit=Decimal("4.00"))
    cache_roots = [ROOT / "eval/reports" / n / "api-cache" for n in (
        "oracle-flash-complete-20260927", "oracle-v4-improvement-20260927", "oracle-structural-20260928-v2",
        "oracle-narrative-20260928")]
    client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA),
        root / "api-cache", max_calls=1500, max_tokens=3500000, reuse_roots=cache_roots,
        before_request=guard.check)
    try:
        baseline = summarize(cases, before, old_v)
        atomic_write_json(root / "baseline-evaluation.json", baseline)
        reports = {}
        for name, config in configs.items():
            out = root / ("screen-"+name)
            traces = run(dataset, cases, config, out, gate, provider, client)
            offline = "baseline" if name == "indexed-baseline" else writer
            atomic_write_json(root / (name+"-pair-audit.json"), evidence_audit(
                _load_run(root / ("offline-"+offline))[1], traces))
            verdicts = judge_run(client, dataset, cases, out, root / (name+"-verdicts.jsonl"))
            reports[name] = summarize(cases, traces, verdicts)
            atomic_write_json(root / (name+"-evaluation.json"), reports[name])
        eligible = [n for n, r in reports.items() if
            r["answerable"]["correct"] >= baseline["answerable"]["correct"]+3 and
            r["unanswerable"]["correct"] >= baseline["unanswerable"]["correct"]]
        selected = max(eligible, key=lambda n: (470*reports[n]["answerable"]["correct"]/50+
            reports[n]["unanswerable"]["correct"], -list(configs).index(n)), default=None)
        atomic_write_json(root / "selection.json", {"selected": selected, "configs": {n:c.to_dict() for n,c in configs.items()}})
        if selected and args.full:
            out = root / ("full-"+selected)
            traces = run(dataset, dataset.cases, configs[selected], out, gate, provider, client)
            validation = validate_submission(out / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
            atomic_write_json(root / "submission-validation.json", validation.to_dict())
            if validation.status != "VALID": raise PilotStopped("Invalid submission")
            verdicts = judge_run(client, dataset, dataset.cases, out, root / "full-verdicts.jsonl")
            atomic_write_json(root / "full-evaluation.json", summarize(dataset.cases, traces, verdicts, exposed_ids=plan["ids"]))
            atomic_write_json(root / "full-comparison.json", _accuracy(old, traces, old_v, verdicts,
                              {c.question_id:c.question_type for c in dataset.cases}))
            atomic_write_json(root / "full-coverage.json", coverage(dataset.cases, traces))
        guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"screen_complete": True, "selected": selected,
            "full_run": "COMPLETED" if selected and args.full else "NOT_RUN", "official_score": "NOT_EVALUATED"})
    finally:
        atomic_write_json(root / "usage.json", client.summary())


if __name__ == "__main__":
    try: main()
    except PilotStopped as exc:
        print(str(exc), flush=True); sys.exit(2)
