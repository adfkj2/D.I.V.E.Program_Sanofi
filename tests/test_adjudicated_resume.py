from decimal import Decimal
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audit_tools"))
from resume_adjudicated_full import remaining_projection, resumable_trace, full


def sample():
    stages = [{"usage": {"input_tokens": 99999, "output_tokens": 99999}},
              {"usage": {"input_tokens": 100, "output_tokens": 20}}]
    return {"s": {"reader": {"detail": json.dumps({"stages": stages})}}}


def test_resume_counts_overlapping_completed_and_screened_work_once():
    result = remaining_projection(sample(), ["s"], ["s", "a", "b", "c"], ["s", "a"], ["s", "b"])
    assert result["remaining_uncached_reader_cases"] == 1
    assert result["remaining_runner_cases"] == 2
    assert result["judge_allowance_cases"] == 4
    assert Decimal(result["reader_review_cny"]) == Decimal("0.00036")
    assert Decimal(result["judge_allowance_cny"]) == Decimal("0.012096")


def test_all_reader_answers_complete_still_reserves_judging():
    result = remaining_projection(sample(), ["s"], ["s", "a"], ["s", "a"], ["s"])
    assert result["remaining_uncached_reader_cases"] == 0
    assert Decimal(result["planning_allowance_cny"]) == Decimal("0.00756")


def test_only_verified_cached_verdicts_reduce_judge_estimate():
    result = remaining_projection(sample(), ["s"], ["s", "a"], ["s", "a"], ["s"], ["s"])
    assert result["judge_allowance_cases"] == 1
    with pytest.raises(full.PilotStopped, match="Unexpected saved case"):
        remaining_projection(sample(), ["s"], ["s", "a"], ["s", "a"], ["s"], ["a"])


def test_unknown_completed_id_cannot_reduce_resume_cost():
    with pytest.raises(full.PilotStopped, match="Unexpected saved case"):
        remaining_projection(sample(), ["s"], ["s", "a"], ["foreign"], ["s"])


def test_malformed_answer_is_kept_as_a_scored_failure_without_resampling():
    assert resumable_trace({"status": "DEGRADED", "reader": {"status": "INVALID"}})
    assert not resumable_trace({"status": "DEGRADED", "reader": {"status": "ERROR"}})
    assert not resumable_trace({"status": "FAILED"})
