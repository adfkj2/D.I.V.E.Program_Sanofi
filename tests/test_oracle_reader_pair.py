"""Offline guards for the v2/v3 reader comparison harness."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest

from dive_memory.oracle.contracts import CANONICAL_ABSTENTION_HYPOTHESIS

_SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "oracle_reader_pair.py"
_SPEC = importlib.util.spec_from_file_location("oracle_reader_pair", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
compare_runs = _MODULE.compare_runs
plan_selection = _MODULE.plan_selection
audit_run_context = _MODULE.audit_run_context


@dataclass(frozen=True)
class Case:
    question_id: str
    question_type: str


def test_plan_is_reproducible_and_reserves_unanswerable_cases() -> None:
    cases = [Case(f"a-{index:02d}", "assistant") for index in range(12)]
    cases += [Case(f"t-{index:02d}", "temporal") for index in range(12)]
    cases += [Case(f"u-{index:02d}_abs", "unanswerable") for index in range(6)]
    first = plan_selection(cases, size=12, abstention=3, seed=7)
    again = plan_selection(list(reversed(cases)), size=12, abstention=3, seed=7)
    assert [case.question_id for case in first] == [case.question_id for case in again]
    assert sum(case.question_id.endswith("_abs") for case in first) == 3
    assert {case.question_type for case in first} == {"assistant", "temporal", "unanswerable"}
    with pytest.raises(ValueError):
        plan_selection(cases, size=12, abstention=7, seed=7)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _run(root: Path, policy: str, prompt: str, *, context: str = "[M] Some fact") -> None:
    manifest = {
        "dataset": {"sha256": "dataset-sha"},
        "resume_key": {"selection_sha256": "selection-sha"},
        "git": {"commit": "same-commit", "dirty": False},
        "configuration": {
            "oracle_pipeline": {"limit": 10, "token_budget": 1500,
                                "reader_policy": policy, "scoring_policy": "balanced"},
            "gate": "semantic", "embedder": "bge-m3",
            "reader_spec": {"provider": "test", "model": "reader", "revision": "rev",
                            "prompt_version": prompt, "temperature": 0.0,
                            "max_tokens": 512, "timeout_seconds": 60},
            "reader_policy": {"name": policy, "prompt_version": prompt},
            "reader_policy_fingerprint": "policy-hash",
            "reader_fingerprint": "reader-hash",
        },
    }
    _write_json(root / "manifest.json", manifest)
    for qid, hypothesis in (("q-1", "Some fact"),
                             ("q-2_abs", CANONICAL_ABSTENTION_HYPOTHESIS)):
        _write_json(root / "traces" / f"{qid}.json", {
            "question_id": qid, "question_date": "2024-01-01", "as_of": "2024-01-01",
            "status": "COMPLETED", "hypothesis": hypothesis,
            "retrieval": {"returned_memory_ids": ["m-1"],
                          "returned_source_refs": [["e-1"]]},
            "packing": {"packed_memory_ids": ["m-1"], "estimated_tokens": 4},
            "context_text": "[M] Some fact", "reader_context_text": context,
            "tokens": {"reader_input": 40 if policy == "evidence_first_v2" else 55},
            "reader": {"status": "ANSWER" if qid == "q-1" else "ABSTAIN",
                       "spec": {"prompt_version": prompt}},
        })


def _verdicts(path: Path, *, correct: dict[str, bool],
              hypothesis_override: str | None = None) -> None:
    rows = [{"_header": True, "schema_version": "dive-judge-verdicts-v1",
             "judge": "exact", "dataset_sha256": "dataset-sha"}]
    for qid, verdict in correct.items():
        hypothesis = hypothesis_override or ("Some fact" if qid == "q-1" else
                                             CANONICAL_ABSTENTION_HYPOTHESIS)
        rows.append({"question_id": qid,
                     "hypothesis_sha256": sha256(hypothesis.encode()).hexdigest()[:16],
                     "correct": verdict})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_pair_requires_identical_actual_evidence_and_reports_saved_accuracy(tmp_path: Path) -> None:
    b, c = tmp_path / "v2", tmp_path / "v3"
    _run(b, "evidence_first_v2", "oracle-grounded-reader-v2")
    _run(c, "evidence_first", "oracle-grounded-reader-v3")
    b_verdicts, c_verdicts = tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    _verdicts(b_verdicts, correct={"q-1": False, "q-2_abs": True})
    _verdicts(c_verdicts, correct={"q-1": True, "q-2_abs": True})
    report = compare_runs(b, c, baseline_verdicts=b_verdicts,
                          candidate_verdicts=c_verdicts)
    assert report["comparable_reader_ablation"] is True
    assert report["evidence_equal_cases"] == 2
    assert report["reader_input_tokens"]["candidate"]["input_total"] == 110
    assert report["accuracy"]["baseline"]["full_set_accuracy"] == .5
    assert report["accuracy"]["candidate"]["full_set_accuracy"] == 1.0
    assert report["accuracy"]["paired_transitions"]["candidate_only"] == 1
    assert report["accuracy"]["baseline"]["response_outcomes"] == {
        "correct_answer": 0,
        "wrong_answer": 1,
        "correct_refusal": 1,
        "wrong_refusal": 0,
        "invalid_output": 0,
        "unjudged": 0,
    }
    assert report["accuracy"]["candidate"]["response_outcomes"] == {
        "correct_answer": 1,
        "wrong_answer": 0,
        "correct_refusal": 1,
        "wrong_refusal": 0,
        "invalid_output": 0,
        "unjudged": 0,
    }
    assert report["official_score"] == "NOT_EVALUATED"


def test_error_fallback_is_invalid_output_not_a_semantic_refusal(tmp_path: Path) -> None:
    b, c = tmp_path / "v2", tmp_path / "v3"
    _run(b, "evidence_first_v2", "oracle-grounded-reader-v2")
    _run(c, "evidence_first", "oracle-grounded-reader-v3")
    for root in (b, c):
        trace_path = root / "traces" / "q-2_abs.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        trace["reader"]["status"] = "TIMEOUT"
        trace["hypothesis_origin"] = "ERROR_FALLBACK"
        _write_json(trace_path, trace)
    b_verdicts, c_verdicts = tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    _verdicts(b_verdicts, correct={"q-1": True, "q-2_abs": True})
    _verdicts(c_verdicts, correct={"q-1": True, "q-2_abs": True})

    report = compare_runs(b, c, baseline_verdicts=b_verdicts,
                          candidate_verdicts=c_verdicts)

    assert report["accuracy"]["baseline"]["response_outcomes"]["invalid_output"] == 1
    assert report["accuracy"]["baseline"]["response_outcomes"]["correct_refusal"] == 0
    assert report["accuracy"]["baseline"]["correct_refusals_on_abstention_cases"] == 0
    assert report["accuracy"]["baseline"]["correct"] == 1
    assert sum(report["accuracy"]["baseline"]["response_outcomes"].values()) == 2


def test_null_invalid_hypothesis_can_only_be_scored_false(tmp_path):
    trace = {"q": {"hypothesis": None, "reader": {"status": "INVALID"}}}
    header = {"_header": True, "dataset_sha256": "data"}
    row = {"question_id": "q", "hypothesis_sha256": sha256(b"null").hexdigest()[:16], "correct": False}
    path = tmp_path / "verdicts.jsonl"
    path.write_text(json.dumps(header) + "\n" + json.dumps(row))
    assert _MODULE._load_verdicts(path, trace, "data")[1] == {"q": False}
    row["correct"] = True
    path.write_text(json.dumps(header) + "\n" + json.dumps(row))
    with pytest.raises(ValueError, match="no matching hypothesis"):
        _MODULE._load_verdicts(path, trace, "data")


def test_equal_over_budget_context_is_not_a_valid_pair(tmp_path):
    b, c = tmp_path / "v2", tmp_path / "v3"
    _run(b, "evidence_first_v2", "oracle-grounded-reader-v2", context="x" * 9000)
    _run(c, "evidence_first", "oracle-grounded-reader-v3", context="x" * 9000)
    report = compare_runs(b, c)
    assert not report["comparable_reader_ablation"]
    assert any("budget" in issue for issue in report["issues"])


def test_dirty_pair_requires_matching_validated_code_snapshots(tmp_path):
    from dive_memory.eval_manifest import code_snapshot
    b, c = tmp_path / "v2", tmp_path / "v3"
    _run(b, "evidence_first_v2", "oracle-grounded-reader-v2")
    _run(c, "evidence_first", "oracle-grounded-reader-v3")
    snapshot = code_snapshot(Path(__file__).resolve().parents[1])
    for root in (b, c):
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["git"]["dirty"] = True
        manifest["configuration"]["code_snapshot"] = snapshot
        _write_json(root / "manifest.json", manifest)
    assert compare_runs(b, c)["comparable_reader_ablation"]
    manifest["configuration"]["code_snapshot"]["sha256"] = "bad-hash"
    _write_json(c / "manifest.json", manifest)
    assert not compare_runs(b, c)["comparable_reader_ablation"]


def test_context_change_and_dirty_worktree_invalidate_pair(tmp_path: Path) -> None:
    b, c = tmp_path / "v2", tmp_path / "v3"
    _run(b, "evidence_first_v2", "oracle-grounded-reader-v2")
    _run(c, "evidence_first", "oracle-grounded-reader-v3", context="Different text")
    manifest = json.loads((c / "manifest.json").read_text(encoding="utf-8"))
    manifest["git"]["dirty"] = True
    _write_json(c / "manifest.json", manifest)
    report = compare_runs(b, c)
    assert report["comparable_reader_ablation"] is False
    assert report["accuracy"] is None
    assert report["evidence_equal_cases"] == 0
    assert any("actual" in issue or "evidence" in issue for issue in report["issues"])
    assert any("dirty" in issue for issue in report["issues"])


def test_stale_judge_verdict_is_rejected(tmp_path: Path) -> None:
    b, c = tmp_path / "v2", tmp_path / "v3"
    _run(b, "evidence_first_v2", "oracle-grounded-reader-v2")
    _run(c, "evidence_first", "oracle-grounded-reader-v3")
    b_verdicts, c_verdicts = tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    _verdicts(b_verdicts, correct={"q-1": True, "q-2_abs": True},
              hypothesis_override="stale")
    _verdicts(c_verdicts, correct={"q-1": True, "q-2_abs": True})
    with pytest.raises(ValueError, match="does not match saved hypothesis"):
        compare_runs(b, c, baseline_verdicts=b_verdicts,
                     candidate_verdicts=c_verdicts)


def test_single_run_audit_keeps_coverage_separate_from_accuracy(tmp_path: Path) -> None:
    _run(tmp_path, "evidence_first_v2", "oracle-grounded-reader-v2")
    trace_path = tmp_path / "traces" / "q-1.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["reader_context_text"] = "x" * 7000
    _write_json(trace_path, trace)
    audit = audit_run_context(tmp_path)
    assert audit["auditable_cases"] == 2
    assert audit["packing_estimated_tokens"]["over_budget_cases"] == 0
    assert audit["reader_context_heuristic_tokens"]["over_budget_cases"] == 1
    assert audit["score_type"] == "NO_ACCURACY_MEASURED"
    assert audit["official_score"] == "NOT_EVALUATED"
