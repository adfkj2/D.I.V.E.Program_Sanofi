"""Predeclared v3/v4/source-coordinate ablation; offline first, bounded API second."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ops"))

from oracle_low_cost_pilot import MeteredClient, PilotStopped, private_get, MODEL, ENDPOINT, EXTRA
from oracle_complete_test import BalanceGuard, judge_run, summarize, read_json
from oracle_reader_pair import plan_selection, _context_signature, _accuracy, _load_run, _load_verdicts, audit_run_context
from oracle_retrieval_ablation import DEFAULT_MODEL, DEFAULT_REVISION, model_provider, make_gate
from dive_memory.oracle.reader import ReaderSpec, ReaderOutcome, GroundedChatReader, reader_policy
from dive_memory.oracle.providers import OpenAICompatibleChatClient
from dive_memory.oracle.contracts import ReaderStatus, HypothesisOrigin
from dive_memory.oracle.runner import OracleRunner, atomic_write_json, trace_filename
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.loader import load_oracle_dataset
from dive_memory.oracle.validator import validate_submission
from dive_memory.eval_manifest import code_snapshot

PREVIOUS = ROOT / "eval/reports/oracle-flash-complete-20260927"
PILOT = ROOT / "eval/reports/oracle-flash-pilot-20-20260927"
ARMS = {"v3": ("evidence_first", False), "v4": ("derived_evidence", False),
        "v4-coordinates": ("derived_evidence", True)}


class OfflineReader:
    def __init__(self, policy):
        self.policy = policy
        self.spec = ReaderSpec("offline", "no-answer-metric", prompt_version=policy.prompt_version)

    def generate(self, request):
        return ReaderOutcome(ReaderStatus.ABSTAIN, None, "OFFLINE_CAPTURE_NOT_AN_ANSWER",
                             origin=HypothesisOrigin.READER_ABSTENTION, calls=0)


def trace_map(root):
    return _load_run(root)[1]


def run_arm(dataset, cases, arm, root, gate, gate_name, client=None):
    name, coords = ARMS[arm]
    policy = reader_policy(name)
    if client is None:
        reader = OfflineReader(policy)
    else:
        client.phase = "reader-" + root.name
        reader = GroundedChatReader(client, ReaderSpec("deepseek", MODEL,
                                    prompt_version=policy.prompt_version, max_tokens=512), policy=policy)
    runner = OracleRunner(dataset, root, reader, gate=gate, gate_name=gate_name,
                          pipeline_config=PipelineConfig(reader_policy_name=name, source_coordinates=coords,
                              scoring_policy_name="balanced", document_selection_name="query_excerpt",
                              document_chunk_search_name="controlled_supplement"), selection=cases,
                          selection_meta={"ids": [case.question_id for case in cases]},
                          progress=lambda event: print(json.dumps({"stage": root.name, **event}), flush=True))
    runner.run(resume=(root / "manifest.json").exists())
    audit = audit_run_context(root)
    atomic_write_json(root / "context-audit.json", audit)
    if audit["auditable_cases"] != len(cases) or audit["reader_context_heuristic_tokens"]["over_budget_cases"]:
        raise PilotStopped("Missing/over-budget evidence; no continued paid run.")


def coverage(cases, traces):
    rows = []
    for case in cases:
        trace = traces[case.question_id]
        formation = trace["formation"]
        returned = {ref for refs in trace["retrieval"]["returned_source_refs"] for ref in refs}
        packed = {ref for view in trace["evidence"]["views"] for ref in view["source_refs"]}
        refs = () if case.should_abstain else case.evidence_refs
        rows.append({"question_id": case.question_id, "question_type": case.question_type,
                     "gold_turns": len(refs),
                     "formed": sum(formation["turn_memory_counts"].get(ref, 0) > 0 for ref in refs),
                     "retrieved": sum(formation["turn_events"].get(ref) in returned for ref in refs),
                     "packed": sum(formation["turn_events"].get(ref) in packed for ref in refs)})
    return {"is_accuracy": False, "overall": {key: sum(row[key] for row in rows)
             for key in ("gold_turns", "formed", "retrieved", "packed")}, "per_case": rows}


def evidence_audit(baseline, candidate, *, allow_coordinates=False):
    if set(baseline) != set(candidate):
        raise PilotStopped("Pair case sets differ.")
    identical = 0
    for qid, before in baseline.items():
        after = candidate[qid]
        # Source formation and the raw retrieval decision must remain fixed.
        for key in ("turn_events", "turn_memory_counts"):
            if before["formation"][key] != after["formation"][key]:
                raise PilotStopped("Formation drift detected.")
        for key in ("returned_memory_ids", "returned_source_refs"):
            if before["retrieval"][key] != after["retrieval"][key]:
                raise PilotStopped("Retrieval drift detected.")
        same = _context_signature(before) == _context_signature(after)
        identical += same
        if not same and not allow_coordinates:
            raise PilotStopped("A prompt-only arm changed its evidence.")
    return {"cases": len(baseline), "identical_context_cases": identical,
            "formation_and_retrieval_equal": True,
            "coordinates_and_budgeted_packing_may_change": allow_coordinates}


def select_candidate(baseline, candidates):
    def numbers(summary):
        return summary["answerable"]["correct"], summary["unanswerable"]["correct"]
    pos, neg = numbers(baseline)
    eligible = {}
    for name, report in candidates.items():
        p, n = numbers(report)
        # Predeclared safety margin, not a significance claim.
        if p >= pos + 2 and n >= neg - 1:
            eligible[name] = 470 * p / 50 + n
    chosen = max(eligible, key=lambda name: (eligible[name], name == "v4")) if eligible else None
    return {"selected": chosen, "eligible_scores": eligible,
            "rule": "At least +2/50 answerable, at most -1/30 abstention; weighted rank, prompt-only tie break."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/reports/oracle-v4-improvement-20260927")
    parser.add_argument("--offline-only", action="store_true")
    args = parser.parse_args()
    root = args.output
    dataset = load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    # Exclude earlier pilot positives and manually diagnosed examples from this
    # round's screening positives. All 30 abstention cases remain mandatory.
    exposed = set((ROOT / "eval/reports/oracle-reader-pilot-20.ids").read_text().splitlines())
    exposed |= set((ROOT / "eval/reports/oracle-reader-pilot-50-all-abstention.ids").read_text().splitlines())
    exposed |= {"982b5123", "c8090214", "e4e14d04", "gpt4_9a159967"}
    pool = [case for case in dataset.cases if case.should_abstain or case.question_id not in exposed]
    cases = plan_selection(pool, size=80, abstention=30, seed=20260928)
    plan = {"dataset_sha256": dataset.sha256, "ids": [case.question_id for case in cases],
            "seed": 20260928, "selection_uses_correctness": False, "arms": ARMS,
            "max_calls": 1600, "max_tokens": 3000000, "stop_balance_cny": "3.00",
            "source_snapshot": code_snapshot(ROOT),
            "selection_rule": "+2/50 answerable minimum, -1/30 abstention maximum; weighted rank, v4 tie",
            "coordinates_offline_gate": "no formed/retrieved change; at most 2 net gold-turn packing losses"}
    # JSON normalization avoids tuple/list mismatches when checking resumes.
    plan = json.loads(json.dumps(plan))
    plan_path = root / "frozen-plan.json"
    if plan_path.exists() and read_json(plan_path) != plan:
        raise PilotStopped("Frozen experiment changed; refuse to mix versions.")
    atomic_write_json(plan_path, plan)
    provider = model_provider(DEFAULT_MODEL, DEFAULT_REVISION, "cuda")
    gate, gate_name = make_gate(provider, argparse.Namespace(gate="semantic", document_admission="all",
                              document_min_chars=200, document_max_chars=8000, gate_cache=1024, gate_batch_size=8))
    for arm in ("v3", "v4-coordinates"):
        run_arm(dataset, cases, arm, root / ("offline-" + arm), gate, gate_name)
    offline = {arm: trace_map(root / ("offline-" + arm)) for arm in ("v3", "v4-coordinates")}
    previous_manifest, previous = _load_run(PREVIOUS / "full-v3")
    before = {case.question_id: previous[case.question_id] for case in cases}
    audit_old = evidence_audit(before, offline["v3"])
    audit_coords = evidence_audit(offline["v3"], offline["v4-coordinates"], allow_coordinates=True)
    cov = {arm: coverage(cases, traces) for arm, traces in offline.items()}
    coord_loss = cov["v3"]["overall"]["packed"] - cov["v4-coordinates"]["overall"]["packed"]
    allow_coords = coord_loss <= 2
    atomic_write_json(root / "offline-comparison.json", {"baseline_replay": audit_old,
                      "coordinate_audit": audit_coords, "coverage": cov,
                      "coordinates_eligible_for_paid_test": allow_coords, "api_calls": 0})
    if args.offline_only:
        print(json.dumps({"offline_completed": True, "coordinate_net_packed_loss": coord_loss}), flush=True)
        return
    key = getpass.getpass("DeepSeek key (hidden, never saved): ")
    if MODEL not in [row["id"] for row in private_get("models", key).get("data", [])]:
        raise PilotStopped("Model unavailable.")
    guard = BalanceGuard(key, root)
    client = MeteredClient(OpenAICompatibleChatClient(ENDPOINT, key, provider="deepseek", extra_body=EXTRA),
                          root / "api-cache", max_calls=1600, max_tokens=3000000,
                          reuse_roots=[PREVIOUS / "api-cache", PILOT / "api-cache"], before_request=guard.check)
    _, all_baseline_verdicts = _load_verdicts(PREVIOUS / "full-verdicts.jsonl", previous, dataset.sha256)
    baseline_verdicts = {case.question_id: all_baseline_verdicts[case.question_id] for case in cases}
    baseline_summary = summarize(cases, before, baseline_verdicts)
    reports = {}
    try:
        for arm in (["v4", "v4-coordinates"] if allow_coords else ["v4"]):
            run_root = root / ("screen-" + arm)
            run_arm(dataset, cases, arm, run_root, gate, gate_name, client)
            traces = trace_map(run_root)
            audit = evidence_audit(before, traces, allow_coordinates=ARMS[arm][1])
            verdicts = judge_run(client, dataset, cases, run_root, root / f"{arm}-verdicts.jsonl")
            reports[arm] = summarize(cases, traces, verdicts)
            paired = _accuracy(before, traces, baseline_verdicts, verdicts,
                               {case.question_id: case.question_type for case in cases})
            atomic_write_json(root / f"{arm}-comparison.json", {"audit": audit, "accuracy": paired})
        selection = select_candidate(baseline_summary, reports)
        atomic_write_json(root / "selection.json", selection)
        print(json.dumps(selection), flush=True)
        if not selection["selected"]:
            atomic_write_json(root / "completion.json", {"screen_complete": True, "full_run": "NOT_RUN_NO_ELIGIBLE_CANDIDATE"})
            guard.check(client.summary()["attempts"], force=True)
            return
        arm = selection["selected"]
        full_root = root / ("full-" + arm)
        run_arm(dataset, dataset.cases, arm, full_root, gate, gate_name, client)
        traces = trace_map(full_root)
        audit = evidence_audit(previous, traces, allow_coordinates=ARMS[arm][1])
        validation = validate_submission(full_root / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
        atomic_write_json(root / "submission-validation.json", validation.to_dict())
        if validation.status != "VALID":
            raise PilotStopped("Submission failed validation.")
        verdicts = judge_run(client, dataset, dataset.cases, full_root, root / "full-verdicts.jsonl")
        summary = summarize(dataset.cases, traces, verdicts, exposed_ids=set(plan["ids"]))
        paired = _accuracy(previous, traces, all_baseline_verdicts, verdicts,
                           {case.question_id: case.question_type for case in dataset.cases})
        atomic_write_json(root / "full-evaluation.json", summary)
        atomic_write_json(root / "full-comparison.json", {"audit": audit, "accuracy": paired,
                          "baseline": str(PREVIOUS / "full-v3"),
                          "caveat": "Saved prior v3 responses, same model ID/judge, not a simultaneous provider-revision-pinned repeat."})
        atomic_write_json(root / "full-coverage.json", coverage(dataset.cases, traces))
        guard.check(client.summary()["attempts"], force=True)
        atomic_write_json(root / "completion.json", {"screen_complete": True, "full_run": "COMPLETED",
                          "selected": arm, "overall": summary["overall"], "official_score": "NOT_EVALUATED"})
        print(json.dumps({"completed": True, "overall": summary["overall"]}), flush=True)
    finally:
        atomic_write_json(root / "usage.json", client.summary())


if __name__ == "__main__":
    try:
        main()
    except PilotStopped as exc:
        print(str(exc), flush=True)
        sys.exit(2)
