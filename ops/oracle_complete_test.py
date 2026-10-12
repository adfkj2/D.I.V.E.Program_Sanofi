"""Frozen 50-case reader screening followed by one full 500-case self-evaluation.

No runtime tuning, no gold sent to the reader, no second 500-case reader arm.
Credentials are hidden input only. Exact paid requests reuse the previous pilot.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import getpass
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ops"))

from oracle_low_cost_pilot import (MeteredClient, PilotStopped, private_get, ENDPOINT, MODEL, EXTRA)
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, make_gate, model_provider
from oracle_reader_pair import compare_runs, audit_run_context
from dive_memory.oracle.reader import GroundedChatReader, ReaderSpec, reader_policy
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, atomic_write_text, trace_filename
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.evaluate import LLMJudge, JudgeSpec, JudgeVerdict
from dive_memory.oracle.validator import validate_submission
from dive_memory.oracle.contracts import CANONICAL_ABSTENTION_HYPOTHESIS
from dive_memory.eval_manifest import code_snapshot

POLICIES = {"v2": "evidence_first_v2", "v3": "evidence_first"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def balance_cny(payload):
    rows = [row for row in payload.get("balance_infos", []) if row.get("currency") == "CNY"]
    if len(rows) != 1:
        raise PilotStopped("A single CNY balance is required for the spending guard.")
    return Decimal(rows[0]["total_balance"])


class BalanceGuard:
    def __init__(self, key, root, *, limit=Decimal("3.00")):
        self.key, self.root, self.limit = key, Path(root), limit
        first = self.root / "balance-before.json"
        current = private_get("user/balance", key)
        if not first.exists():
            atomic_write_json(first, current)
        self.initial = balance_cny(read_json(first))
        self.last_checked = None
        self.check(0, force=True)

    def check(self, attempts, *, force=False):
        if not force and (attempts % 20 or attempts == self.last_checked):
            return
        payload = private_get("user/balance", self.key)
        remaining = balance_cny(payload)
        spent = self.initial - remaining
        atomic_write_json(self.root / "balance-latest.json", {
            "checked_at": datetime.now(timezone.utc).isoformat(), "attempts": attempts,
            "initial_cny": str(self.initial), "remaining_cny": str(remaining),
            "observed_spent_cny": str(spent), "stop_threshold_cny": str(self.limit),
            "note": "Balance can lag or include other account usage; checked every 20 paid requests.",
        })
        self.last_checked = attempts
        if not payload.get("is_available") or remaining <= 0 or spent >= self.limit:
            raise PilotStopped("Balance spending threshold reached; no further paid request.")


def invalid_trace(trace):
    return (trace.get("reader", {}).get("status") in {"INVALID", "ERROR", "TIMEOUT"}
            or trace.get("hypothesis_origin") == "ERROR_FALLBACK")


def score_trace(judge, case, trace):
    hypothesis = trace.get("hypothesis")
    if invalid_trace(trace):
        return JudgeVerdict(False, "INVALID_OUTPUT_RULE")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise PilotStopped("Non-failure trace lacks a nonempty answer.")
    result = judge.judge(question=case.question, reference=case.answer, hypothesis=hypothesis,
                         question_type="abstention" if case.should_abstain else case.question_type)
    if result.correct is None:
        raise PilotStopped("Judge returned no valid verdict; stopping without automatic retry.")
    return result


def judge_run(client, dataset, cases, run_root, output):
    client.phase = "judge-" + run_root.name
    spec = JudgeSpec(provider="deepseek", model=MODEL, max_tokens=128,
                     caveat="Same-family self-judging, non-official pilot only.")
    judge = LLMJudge(client, spec)
    header = {"_header": True, "schema_version": "dive-judge-verdicts-v1",
              "judge": spec.to_dict(), "dataset_sha256": dataset.sha256,
              "invalid_output_policy": "deterministic-failure-false; null digest is SHA256 of literal null"}
    rows = [header]
    for index, case in enumerate(cases, 1):
        trace = read_json(run_root / "traces" / trace_filename(case.question_id))
        hypothesis = trace.get("hypothesis")
        verdict = score_trace(judge, case, trace)
        rows.append({"question_id": case.question_id,
                     "hypothesis_sha256": hashlib.sha256(("null" if hypothesis is None else hypothesis).encode()).hexdigest()[:16],
                     "correct": verdict.correct, "raw": verdict.raw, "error": verdict.error})
        atomic_write_text(output, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        if index % 20 == 0 or index == len(cases):
            print(json.dumps({"stage": client.phase, "completed": index, "total": len(cases)}), flush=True)
    return {row["question_id"]: row["correct"] for row in rows[1:]}


def choose_arm(accuracy):
    """Predeclared screen: weight answerability strata 470:30, tie to baseline.

    A ranking statistic, not an extrapolated benchmark score. Both arms retain
    their per-type regressions in the report irrespective of selection.
    """
    scores = {}
    for name, label in (("v2", "baseline"), ("v3", "candidate")):
        arm = accuracy[label]
        abs_n = arm["abstention_cases"]
        positive_n = accuracy["paired_cases"] - abs_n
        if abs_n != 30 or positive_n != 20 or not accuracy["judge_complete"]:
            raise PilotStopped("Screen must contain all 30 abstention plus 20 answerable cases, fully judged.")
        positive_correct = arm["correct"] - arm["correct_on_abstention_cases"]
        scores[name] = (470 * positive_correct / positive_n
                        + 30 * arm["correct_on_abstention_cases"] / abs_n)
    chosen = "v3" if scores["v3"] > scores["v2"] else "v2"
    return {"selected_arm": chosen, "selection_statistic": scores,
            "rule": "470*(correct_answerable/20)+30*(correct_unanswerable/30); ties select v2",
            "warning": "Screening statistic only, not an estimated or official 500-case accuracy."}


def summarize(cases, traces, verdicts, *, exposed_ids=()):
    rows = []
    for case in cases:
        trace, correct = traces[case.question_id], verdicts[case.question_id]
        invalid = invalid_trace(trace)
        refused = trace.get("hypothesis") == CANONICAL_ABSTENTION_HYPOTHESIS
        if invalid:
            correct, outcome = False, "invalid_output"
        elif correct is None:
            outcome = "unjudged"
        else:
            outcome = ("correct_" if correct else "wrong_") + ("refusal" if refused else "answer")
        rows.append({"question_id": case.question_id, "question_type": case.question_type,
                     "should_abstain": case.should_abstain, "correct": correct,
                     "outcome": outcome, "previously_exposed": case.question_id in exposed_ids,
                     "question": case.question, "reference": case.answer,
                     "hypothesis": trace.get("hypothesis")})

    def block(items):
        hits = sum(row["correct"] is True for row in items)
        complete = all(isinstance(row["correct"], bool) for row in items)
        return {"cases": len(items), "correct": hits, "fully_judged": complete,
                "accuracy": hits / len(items) if items and complete else None,
                "outcomes": dict(Counter(row["outcome"] for row in items))}

    return {"score_type": "SELF_REPORTED_NON_OFFICIAL", "official_score": "NOT_EVALUATED",
            "overall": block(rows),
            "answerable": block([row for row in rows if not row["should_abstain"]]),
            "unanswerable": block([row for row in rows if row["should_abstain"]]),
            "by_question_type": {name: block([row for row in rows if row["question_type"] == name])
                                 for name in sorted({row["question_type"] for row in rows})},
            "screen_exposed": block([row for row in rows if row["previously_exposed"]]),
            "outside_current_screen": block([row for row in rows if not row["previously_exposed"]]),
            "caveat": "Outside-screen is not guaranteed untouched: historical development used this public dataset. Same-family judge; no official script.",
            "per_question": rows}


def run_reader(dataset, cases, arm, root, client, gate, gate_name, selection_meta):
    client.phase = "reader-" + root.name
    policy = reader_policy(POLICIES[arm])
    reader = GroundedChatReader(client, ReaderSpec(provider="deepseek", model=MODEL,
                                prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    runner = OracleRunner(dataset, root, reader, gate=gate, gate_name=gate_name,
                          pipeline_config=PipelineConfig(reader_policy_name=POLICIES[arm],
                              scoring_policy_name="balanced", document_selection_name="query_excerpt",
                              document_chunk_search_name="controlled_supplement"),
                          selection=cases, selection_meta=selection_meta,
                          progress=lambda event: print(json.dumps({"stage": client.phase, **event}), flush=True))
    runner.run(resume=(root / "manifest.json").exists())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-flash-complete-20260927")
    args = parser.parse_args()
    root = args.output
    dataset_path = ROOT / "eval/external/longmemeval/longmemeval_oracle.json"
    dataset = load_oracle_dataset(dataset_path)
    ids_path = ROOT / "eval/reports/oracle-reader-pilot-50-all-abstention.ids"
    plan = read_json(ids_path.with_suffix(".ids.plan.json"))
    ids_text = ids_path.read_text(encoding="utf-8")
    if dataset.sha256 != plan["dataset_sha256"] or hashlib.sha256(ids_text.encode()).hexdigest() != plan["ids_sha256"]:
        raise PilotStopped("Dataset or fixed 50-case selection mismatch.")
    cases = [dataset.by_id()[qid] for qid in ids_text.splitlines() if qid]
    frozen = {"schema": "dive-frozen-full-test-v1", "dataset_sha256": dataset.sha256,
              "screen_ids": [case.question_id for case in cases], "model": MODEL,
              "reader_max_tokens": 512, "judge_max_tokens": 128, "extra_body": EXTRA,
              "screen_selection_rule": "470*answerable_accuracy+30*unanswerable_accuracy; tie=v2",
              "max_paid_calls": 1800, "max_tokens": 3000000, "balance_stop_cny": "3.00",
              "code_snapshot": code_snapshot(ROOT)}
    frozen_path = root / "frozen-plan.json"
    if frozen_path.exists() and read_json(frozen_path) != frozen:
        raise PilotStopped("Frozen plan/code changed; do not mix or rerun this test.")
    atomic_write_json(frozen_path, frozen)
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if MODEL not in [row["id"] for row in private_get("models", key).get("data", [])]:
        raise PilotStopped("Requested model not available.")
    guard = BalanceGuard(key, root)
    client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA),
                          root / "api-cache", max_calls=1800, max_tokens=3000000,
                          reuse_roots=[ROOT / "eval/reports/oracle-flash-pilot-20-20260927/api-cache"],
                          before_request=guard.check)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, gate_name = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
                              document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    verdict_paths = {}
    try:
        for arm in POLICIES:
            run_reader(dataset, cases, arm, root / ("screen-" + arm), client, gate, gate_name,
                       {"ids_sha256": plan["ids_sha256"], "seed": plan["seed"]})
        audit = compare_runs(root / "screen-v2", root / "screen-v3", dataset_path=dataset_path)
        atomic_write_json(root / "screen-evidence-audit.json", audit)
        if not audit["comparable_reader_ablation"] or audit["cases_compared"] != 50:
            raise PilotStopped("Screen evidence audit failed; no paid judge/full run.")
        for arm in POLICIES:
            verdict_paths[arm] = root / ("screen-" + arm + "-verdicts.jsonl")
            judge_run(client, dataset, cases, root / ("screen-" + arm), verdict_paths[arm])
        comparison = compare_runs(root / "screen-v2", root / "screen-v3", dataset_path=dataset_path,
                                 baseline_verdicts=verdict_paths["v2"], candidate_verdicts=verdict_paths["v3"])
        atomic_write_json(root / "screen-comparison.json", comparison)
        selection = choose_arm(comparison["accuracy"])
        atomic_write_json(root / "selection.json", selection)
        print(json.dumps({"screen_finished": True, **selection}), flush=True)
        guard.check(client.summary()["attempts"], force=True)
        arm = selection["selected_arm"]
        full_root = root / ("full-" + arm)
        run_reader(dataset, dataset.cases, arm, full_root, client, gate, gate_name,
                   {"mode": "all-500", "selection_rule": selection["rule"]})
        context_audit = audit_run_context(full_root)
        atomic_write_json(root / "full-context-audit.json", context_audit)
        if context_audit["reader_context_heuristic_tokens"]["over_budget_cases"]:
            raise PilotStopped("Full reader-visible budget audit failed; stop before paid judging.")
        validation = validate_submission(full_root / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
        atomic_write_json(root / "submission-validation.json", validation.to_dict())
        if validation.status != "VALID":
            raise PilotStopped("Full submission invalid; stop before paid judging.")
        verdicts = judge_run(client, dataset, dataset.cases, full_root, root / "full-verdicts.jsonl")
        traces = {case.question_id: read_json(full_root / "traces" / trace_filename(case.question_id)) for case in dataset.cases}
        exposed = set(ids_text.splitlines()) | set((ROOT / "eval/reports/oracle-reader-pilot-20.ids").read_text().splitlines())
        result = summarize(dataset.cases, traces, verdicts, exposed_ids=exposed)
        result["selected_arm"] = arm
        result["dataset_sha256"] = dataset.sha256
        result["context_auditable_cases"] = context_audit["auditable_cases"]
        atomic_write_json(root / "full-evaluation.json", result)
        guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"completed": True, "selected_arm": arm,
                          "full_cases": 500, "official_score": "NOT_EVALUATED", "usage": client.summary()})
        print(json.dumps({"completed": True, "overall": result["overall"], "usage": client.summary()}), flush=True)
    finally:
        atomic_write_json(root / "usage.json", client.summary())


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
