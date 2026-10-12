"""Run the optional recovery policy over all frozen reader-stage outcomes.

No network client exists here. Generation and scoring reuse are recorded as
replay, not independent model samples. Routing finishes before verdicts load.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "ops"), str(ROOT / "audit_tools")]
import oracle_adjudicated_full as full
from dive_memory.oracle.adjudicated_reader import AdjudicatedReader, FORMAT_RECOVERY_VERSION
from dive_memory.oracle.contracts import ReaderStatus, HypothesisOrigin
from dive_memory.oracle.reader import ReaderOutcome, TokenUsage
from dive_memory.oracle.runner import atomic_write_text, trace_filename
from audit_adjudicated_result import audit_citations, audit_submission


def restore_outcome(saved):
    value = dict(saved)
    value["status"] = ReaderStatus(value["status"])
    value["origin"] = HypothesisOrigin(value["origin"]) if value["origin"] else None
    value["usage"] = TokenUsage(**value["usage"])
    for key in ("citations", "missing_requirements", "attempt_errors"):
        value[key] = tuple(value[key])
    return ReaderOutcome(**value)


class SavedStage:
    def __init__(self, outcome, spec, policy):
        self.outcome, self.spec, self.policy = outcome, spec, policy
        self.used = False

    def generate(self, request):
        if self.used or self.outcome is None:
            raise ValueError("Policy requested an unavailable stage; replay stopped")
        self.used = True
        return self.outcome


def replay_reader(saved, *, specs, policy):
    stages = json.loads(saved["detail"])["stages"]
    readers = [SavedStage(restore_outcome(stages[i]) if i < len(stages) else None, spec, policy)
               for i, spec in enumerate(specs)]
    reader = AdjudicatedReader(*readers, recover_invalid_secondary=True)
    result = reader.generate(object())
    if sum(stage.used for stage in readers) != len(stages):
        raise ValueError("Replay did not consume exactly the recorded stages")
    recovered = json.loads(result.detail)["route"] == "primary_after_secondary_invalid"
    if not recovered:
        before = {k: v for k, v in saved.items() if k not in {"spec", "policy_version"}}
        after = {k: v for k, v in result.to_dict().items() if k != "policy_version"}
        if before != after:
            raise ValueError("An unaffected reader outcome changed")
    return result, reader.spec, recovered


def main():
    parent = ROOT / "eval/reports/oracle-adjudicated-full-20261004"
    baseline = full.PREVIOUS
    output = ROOT / "eval/reports/oracle-format-recovery-20261007"
    audit = full.read_json(parent / "post-run-audit-20261006.json")
    if not audit.get("audit_passed") or audit.get("cases") != 500:
        raise ValueError("Parent full run has not passed independent audit")
    dataset = full.load_oracle_dataset(ROOT / "eval/external/longmemeval/longmemeval_oracle.json")
    dataset.require_full_benchmark()
    manifest, traces = full._load_run(parent / "full-adjudicated")
    if (set(traces) != set(dataset.question_ids)
            or manifest["dataset"]["sha256"] != dataset.sha256):
        raise ValueError("Parent dataset or coverage mismatch")
    snapshot = full.code_snapshot(ROOT)
    parent_digests = {case.question_id: sha256((parent / "full-adjudicated/traces" /
        trace_filename(case.question_id)).read_bytes()).hexdigest() for case in dataset.cases}
    plan = {"dataset_sha256": dataset.sha256, "source_snapshot": snapshot,
        "replay_driver_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "reader_version": FORMAT_RECOVERY_VERSION, "ids": list(dataset.question_ids),
        "selection_uses_correctness": False, "parent_trace_sha256": parent_digests,
        "new_api_calls": 0, "execution_mode": "saved_stage_replay",
        "policy": "Retain validated primary ANSWER/ABSTAIN only if secondary is INVALID; all other routing unchanged.",
        "aggregate_budget": full.read_json(ROOT / "eval/reports/oracle-road-to-90-budget.json")}
    if (output / "frozen-plan.json").exists() and full.read_json(output / "frozen-plan.json") != plan:
        raise ValueError("Frozen replay identity changed")
    full.atomic_write_json(output / "frozen-plan.json", plan)
    policy = full.CONFIG.policy()
    specs = [full.ReaderSpec("deepseek", full.MODEL, prompt_version=policy.prompt_version, max_tokens=512),
             full.ReaderSpec("deepseek", full.MODEL, prompt_version=policy.prompt_version,
                             max_tokens=full.OUTPUT_CAP, timeout_seconds=120),
             full.ReaderSpec("deepseek", full.MODEL, prompt_version=full.REFEREE_VERSION, max_tokens=1024)]
    replay, recovered_ids, submission = {}, [], []
    for case in dataset.cases:
        qid = case.question_id
        record = deepcopy(traces[qid])
        outcome, spec, recovered = replay_reader(record["reader"], specs=specs, policy=policy)
        record["reader"] = {**outcome.to_dict(), "spec": spec.to_dict()}
        if recovered:
            recovered_ids.append(qid)
            record.update(status="COMPLETED", failure=None, hypothesis=outcome.resolved_hypothesis,
                          hypothesis_origin=outcome.origin.value)
        record["replay"] = {"parent_trace_sha256": parent_digests[qid], "new_api_calls": 0,
                            "timing_and_usage_are_parent_measurements": True}
        audit_citations(record)
        replay[qid] = record
        full.atomic_write_json(output / "traces" / trace_filename(qid), record)
        submission.append({"question_id": qid, "hypothesis": outcome.resolved_hypothesis})
    full.evidence_audit(traces, replay)
    audit_submission(submission, replay)
    atomic_write_text(output / "submission.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in submission))
    validation = full.validate_submission(output / "submission.jsonl", expected_ids=dataset.question_ids, require_full=True)
    if validation.status != "VALID":
        raise ValueError("Replay submission invalid")
    full.atomic_write_json(output / "submission-validation.json", validation.to_dict())
    replay_manifest = {"schema_version": "dive-saved-stage-replay-v1", "dataset": manifest["dataset"],
        "configuration": {**manifest["configuration"], "code_snapshot": snapshot,
                          "reader_spec": spec.to_dict(), "reader_fingerprint": spec.fingerprint()},
        "execution_mode": "saved_stage_replay", "new_api_calls": 0,
        "parent_run": str(parent), "stage_timing_and_usage_are_inherited": True}
    full.atomic_write_json(output / "manifest.json", replay_manifest)
    # Evaluation is downstream of the uniform runtime policy and cannot route it.
    _, old = full._load_run(baseline / "full-wide-indexed")
    ph, pv = full._load_verdicts(parent / "full-verdicts.jsonl", traces, dataset.sha256)
    bh, bv = full._load_verdicts(baseline / "full-verdicts.jsonl", old, dataset.sha256)
    if ph != bh:
        raise ValueError("Cached judge specifications differ")
    rows = [ph]
    for case in dataset.cases:
        qid = case.question_id
        source, verdict = (old[qid], bv[qid]) if qid in recovered_ids else (traces[qid], pv[qid])
        if (source["hypothesis"] != replay[qid]["hypothesis"]
                or source["reader"]["status"] != replay[qid]["reader"]["status"]):
            raise ValueError("Saved verdict is not bound to the replayed answer")
        text = replay[qid]["hypothesis"]
        rows.append({"question_id": qid, "correct": verdict,
                     "hypothesis_sha256": sha256(("null" if text is None else text).encode()).hexdigest()[:16],
                     "raw": "VERIFIED_CACHED_VERDICT", "error": None})
    atomic_write_text(output / "full-verdicts.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    _, verdicts = full._load_verdicts(output / "full-verdicts.jsonl", replay, dataset.sha256)
    exposed = full.read_json(full.SCREEN / "frozen-plan.json")["ids"]
    report = full.summarize(dataset.cases, replay, verdicts, exposed_ids=exposed)
    report["execution_mode"] = "saved_stage_replay_not_independent_model_samples"
    full.atomic_write_json(output / "full-evaluation.json", report)
    types = {c.question_id: c.question_type for c in dataset.cases}
    full.atomic_write_json(output / "parent-comparison.json", full._accuracy(traces, replay, pv, verdicts, types))
    full.atomic_write_json(output / "baseline-comparison.json", full._accuracy(old, replay, bv, verdicts, types))
    if full.code_snapshot(ROOT) != snapshot:
        raise ValueError("Source drift during replay")
    full.atomic_write_json(output / "completion.json", {"full_replay_complete": True, "cases": 500,
        "recovered_format_ids": recovered_ids, "correct": report["overall"]["correct"],
        "new_api_calls": 0, "all_citations_checked": True, "evidence_unchanged": True,
        "target_reached": report["overall"]["correct"] >= 450, "official_score": "NOT_EVALUATED",
        "caveat": "Uniform runtime policy replay over existing model outcomes and exact matching judgments; not a new independent model sample."})
    print(json.dumps({"score": report["overall"], "format_recoveries": len(recovered_ids), "new_api_calls": 0}))


if __name__ == "__main__":
    main()
