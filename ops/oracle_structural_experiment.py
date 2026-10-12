"""Frozen structural ablation; free coverage first, at most two paid candidates.

Privileged source diagnostic is evaluation-only and never produces a submission.
Normal arms keep gold outside ingest, retrieval, packing and reading.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache
import getpass
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops")]
from oracle_low_cost_pilot import MeteredClient, PilotStopped, private_get, MODEL, ENDPOINT, EXTRA
from oracle_complete_test import BalanceGuard, judge_run, summarize, read_json, score_trace
from oracle_reader_pair import plan_selection, _load_run, _load_verdicts, _accuracy, audit_run_context
from oracle_v4_experiment import OfflineReader, coverage, evidence_audit
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.reader import ReaderSpec, GroundedChatReader, reader_policy, OracleReaderRequest
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.runner import OracleRunner, atomic_write_json
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.validator import validate_submission
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.oracle.evaluate import LLMJudge, JudgeSpec
from dive_memory.models import Memory, RetrievalItem, EvidenceState
from dive_memory.token_budget import ApproximateTokenCounter
from dive_memory.eval_manifest import code_snapshot

PREVIOUS = ROOT / "eval/reports/oracle-flash-complete-20260927"
V4 = ROOT / "eval/reports/oracle-v4-improvement-20260927"
BASE = PipelineConfig(scoring_policy_name="balanced", document_selection_name="query_excerpt",
                      document_chunk_search_name="controlled_supplement")
ARMS = {
    "baseline": BASE,
    "episodes": replace(BASE, episodic_retention=True),
    "semantic": replace(BASE, semantic_retrieval_only=True),
    "episodes-semantic": replace(BASE, episodic_retention=True, semantic_retrieval_only=True),
    "recovery": replace(BASE, episodic_retention=True, semantic_retrieval_only=True, evidence_recovery=True),
    "roomy": replace(BASE, episodic_retention=True, semantic_retrieval_only=True,
                      evidence_recovery=True, token_budget=3000),
}


class CachedEmbedder:
    """Text-vector cache only; never stores user memories or question labels."""
    def __init__(self, base):
        self.base = base
        self.embed_result = lru_cache(maxsize=4096)(base.embed_result)

    def __getattr__(self, name):
        return getattr(self.base, name)

    def embed(self, text):
        return list(self.embed_result(text).vector)


def cov_metrics(report):
    by_type = defaultdict(int)
    complete = 0
    for row in report["per_case"]:
        by_type[row["question_type"]] += row["packed"]
        complete += row["gold_turns"] > 0 and row["packed"] == row["gold_turns"]
    return {**report["overall"], "complete_positive_cases": complete, "by_type_packed": dict(by_type)}


def shortlist(metrics):
    b = metrics["baseline"]
    eligible = []
    for name, m in metrics.items():
        if name == "baseline":
            continue
        # Small net gains must not conceal major type regressions.
        if m["packed"] < b["packed"] + 2 or m["complete_positive_cases"] < b["complete_positive_cases"]:
            continue
        if any(m["by_type_packed"].get(t, 0) < n - 2 for t, n in b["by_type_packed"].items()):
            continue
        eligible.append(name)
    # Prefer fewer moving parts on ties; at most two paid screens.
    return sorted(eligible, key=lambda n: (-metrics[n]["complete_positive_cases"],
                  -metrics[n]["packed"], list(ARMS).index(n)))[:2]


def choose_paid(baseline, reports):
    eligible = {n: r for n, r in reports.items()
                if r["answerable"]["correct"] >= baseline["answerable"]["correct"] + 2
                and r["unanswerable"]["correct"] >= baseline["unanswerable"]["correct"] - 1}
    return max(eligible, key=lambda n: (470 * eligible[n]["answerable"]["correct"] / 50
               + eligible[n]["unanswerable"]["correct"], -list(ARMS).index(n)), default=None)


def run_arm(dataset, cases, arm, root, gate, provider, client=None):
    config = ARMS[arm]
    policy = config.policy()
    reader = OfflineReader(policy) if client is None else GroundedChatReader(
        client, ReaderSpec("deepseek", MODEL, prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    if client:
        client.phase = "reader-" + root.name
    semantic = config.semantic_retrieval_only
    runner = OracleRunner(dataset, root, reader, gate=gate, gate_name="semantic-utility-v2.1",
        embedder=provider if semantic else None,
        embedder_name=f"{DEFAULT_MODEL}@{DEFAULT_REVISION};max_length=512" if semantic else "deterministic-test-provider (non-semantic)",
        pipeline_config=config, selection=cases, selection_meta={"ids": [c.question_id for c in cases]},
        progress=lambda event: print(json.dumps({"stage": root.name, **event}), flush=True))
    runner.run(resume=(root / "manifest.json").exists())
    audit = audit_run_context(root)
    atomic_write_json(root / "context-audit.json", audit)
    if audit["auditable_cases"] != len(cases) or audit["reader_context_heuristic_tokens"]["over_budget_cases"]:
        raise PilotStopped("Incomplete or over-budget context audit.")
    traces = _load_run(root)[1]
    if any(t.get("status") == "FAILED" for t in traces.values()):
        raise PilotStopped("Runtime stage failure; do not pay for partial evidence.")
    return traces


def diagnostic_plan(cases):
    """Evaluator-only annotated-source condition; no reference answer enters prompt."""
    plans = []
    for case in cases:
        if case.should_abstain:
            continue
        items = []
        for turn in case.chronological_turns:
            if turn.ref not in case.evidence_refs:
                continue
            mid = f"diagnostic-{len(items)}"
            memory = Memory(mid, "privileged-evaluation-only", "episode", EvidenceState.OBSERVATION,
                turn.content, {"role": turn.role}, valid_from=turn.occurred_at_iso)
            items.append(RetrievalItem(memory, 1.0, ["privileged-gold-source"], [turn.ref]))
        text = render_evidence_block(build_evidence_views(items, question=case.question,
                                     question_date=case.visibility_cutoff_iso))
        if items and ApproximateTokenCounter().count(text) <= 1500:
            plans.append((case, OracleReaderRequest(case.question, case.visibility_cutoff_iso,
                          case.visibility_cutoff_iso, tuple(items), text)))
    # Fixed deterministic stratified selection among cases whose full annotated turns fit.
    selected = {c.question_id for c in plan_selection([c for c, _ in plans],
                size=min(18, len(plans)), abstention=0, seed=20260929)} if plans else set()
    return [(c, req) for c, req in plans if c.question_id in selected]


def run_diagnostic(plans, client, root, old, verdicts):
    policy = reader_policy("evidence_first")
    reader = GroundedChatReader(client, ReaderSpec("deepseek", MODEL,
                                prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    judge = LLMJudge(client, JudgeSpec("deepseek", MODEL, max_tokens=128))
    rows = []
    for case, req in plans:
        target = root / "diagnostic" / f"{case.question_id}.json"
        if target.exists():
            rows.append(read_json(target)); continue
        client.phase = "reader-privileged-diagnostic"
        outcome = reader.generate(req)
        trace = {"reader": outcome.to_dict(),
                 "hypothesis": outcome.resolved_hypothesis if outcome.origin else None}
        client.phase = "judge-privileged-diagnostic"
        verdict = score_trace(judge, case, trace)
        row = {"question_id": case.question_id, "condition": "PRIVILEGED_SOURCE_DIAGNOSTIC_NOT_SUBMITTABLE",
               "uses_gold_source_labels": True, "uses_reference_answer_in_reader": False,
               "reader_context_text": req.context_text, **trace,
               "baseline_correct": verdicts[case.question_id], "correct": verdict.correct,
               "judge_raw": verdict.raw}
        atomic_write_json(target, row); rows.append(row)
    atomic_write_json(root / "diagnostic-summary.json", {
        "condition": "PRIVILEGED_SOURCE_DIAGNOSTIC_NOT_SYSTEM_ACCURACY",
        "cases": len(rows), "baseline_correct": sum(r["baseline_correct"] for r in rows),
        "diagnostic_correct": sum(r["correct"] for r in rows),
        "caveat": "Annotated complete turns that fit 1500 approximate tokens; selection bias, not an upper-bound proof or deployable pipeline.",
        "per_case": [{"question_id": r["question_id"], "baseline_correct": r["baseline_correct"],
                      "correct": r["correct"]} for r in rows]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-structural-20260928")
    args = parser.parse_args(); root = args.output
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    exposed = set(read_json(V4 / "frozen-plan.json")["ids"])
    for name in ("oracle-reader-pilot-20.ids", "oracle-reader-pilot-50-all-abstention.ids"):
        exposed.update((ROOT / "eval/reports" / name).read_text().splitlines())
    exposed.update({"982b5123", "c8090214", "e4e14d04", "gpt4_9a159967", "eaca4986", "ed4ddc30"})
    pool = [c for c in dataset.cases if c.should_abstain or c.question_id not in exposed]
    cases = plan_selection(pool, size=80, abstention=30, seed=20260929)
    plans = diagnostic_plan(cases)
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": code_snapshot(ROOT),
        "ids": [c.question_id for c in cases], "arms": {n: c.to_dict() for n,c in ARMS.items()},
        "diagnostic_ids": [c.question_id for c,_ in plans], "selection_uses_correctness": False,
        "offline_selection": "packed +2, complete cases nondecreasing, per-type packed loss <=2; best two",
        "paid_selection": "answerable +2/50, unanswerable loss <=1/30; weighted rank; simpler ties",
        "max_new_calls": 1600, "max_tokens": 3500000, "balance_stop_cny": "3.00"}
    target = root / "frozen-plan.json"
    if target.exists() and read_json(target) != plan:
        raise PilotStopped("Frozen sources/configuration changed; do not mix runs.")
    atomic_write_json(target, plan)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, _ = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
        document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    provider = CachedEmbedder(provider)
    old = _load_run(PREVIOUS / "full-v3")[1]
    _, old_verdicts = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", old, dataset.sha256)
    before = {c.question_id: old[c.question_id] for c in cases}
    metrics = {}
    for arm in ARMS:
        traces = run_arm(dataset, cases, arm, root / ("offline-" + arm), gate, provider)
        if arm == "baseline":
            atomic_write_json(root / "baseline-replay-audit.json", evidence_audit(before, traces))
        cov = coverage(cases, traces)
        atomic_write_json(root / f"{arm}-coverage.json", cov)
        metrics[arm] = cov_metrics(cov)
    candidates = shortlist(metrics)
    atomic_write_json(root / "offline-summary.json", {"metrics": metrics, "shortlist": candidates,
        "score_type": "SOURCE_COVERAGE_NOT_ACCURACY"})
    print(json.dumps({"offline_complete": True, "metrics": metrics, "shortlist": candidates}), flush=True)
    if args.offline_only: return
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if MODEL not in [r["id"] for r in private_get("models", key).get("data", [])]:
        raise PilotStopped("Model unavailable")
    guard = BalanceGuard(key, root)
    client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA),
        root / "api-cache", max_calls=1600, max_tokens=3500000,
        reuse_roots=[PREVIOUS / "api-cache", V4 / "api-cache"], before_request=guard.check)
    try:
        run_diagnostic(plans, client, root, old, old_verdicts)
        baseline = summarize(cases, before, old_verdicts)
        atomic_write_json(root / "baseline-screen-evaluation.json", baseline)
        reports = {}
        for arm in candidates:
            out = root / ("screen-" + arm)
            traces = run_arm(dataset, cases, arm, out, gate, provider, client)
            evidence_audit(_load_run(root / ("offline-" + arm))[1], traces)
            verdicts = judge_run(client, dataset, cases, out, root / f"{arm}-verdicts.jsonl")
            reports[arm] = summarize(cases, traces, verdicts)
            atomic_write_json(root / f"{arm}-evaluation.json", reports[arm])
            atomic_write_json(root / f"{arm}-comparison.json", _accuracy(before, traces, old_verdicts,
                verdicts, {c.question_id: c.question_type for c in cases}))
        selected = choose_paid(baseline, reports)
        atomic_write_json(root / "selection.json", {"selected": selected, "candidates": candidates})
        if selected:
            out = root / ("full-" + selected)
            traces = run_arm(dataset, dataset.cases, selected, out, gate, provider, client)
            validation = validate_submission(out / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
            atomic_write_json(root / "submission-validation.json", validation.to_dict())
            if validation.status != "VALID": raise PilotStopped("Invalid full submission")
            verdicts = judge_run(client, dataset, dataset.cases, out, root / "full-verdicts.jsonl")
            report = summarize(dataset.cases, traces, verdicts, exposed_ids=plan["ids"])
            atomic_write_json(root / "full-evaluation.json", report)
            atomic_write_json(root / "full-comparison.json", _accuracy(old, traces, old_verdicts,
                verdicts, {c.question_id: c.question_type for c in dataset.cases}))
            atomic_write_json(root / "full-coverage.json", coverage(dataset.cases, traces))
        guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"screen_complete": True, "selected": selected,
            "full_run": "COMPLETED" if selected else "NOT_RUN_NO_ELIGIBLE_CANDIDATE", "official_score": "NOT_EVALUATED"})
    finally:
        atomic_write_json(root / "usage.json", client.summary())


if __name__ == "__main__":
    try: main()
    except PilotStopped as exc:
        print(str(exc), flush=True); sys.exit(2)
