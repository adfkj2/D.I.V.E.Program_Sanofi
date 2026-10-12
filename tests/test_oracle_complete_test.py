from decimal import Decimal
from types import SimpleNamespace
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_complete_test import balance_cny, choose_arm, score_trace, summarize, PilotStopped


def test_selection_does_not_overweight_abstention_stress_cases():
    accuracy = {"paired_cases": 50, "judge_complete": True,
                "baseline": {"abstention_cases": 30, "correct": 18, "correct_on_abstention_cases": 0},
                "candidate": {"abstention_cases": 30, "correct": 45, "correct_on_abstention_cases": 30}}
    # v3 has more raw correct on the enriched sample, but loses 3/20 positives.
    assert choose_arm(accuracy)["selected_arm"] == "v2"
    accuracy["candidate"] = dict(accuracy["baseline"])
    assert choose_arm(accuracy)["selected_arm"] == "v2"
    accuracy["paired_cases"] = 49
    with pytest.raises(PilotStopped):
        choose_arm(accuracy)


def test_invalid_trace_is_free_failure_and_not_correct_refusal():
    class NoJudge:
        def judge(self, **kwargs):
            raise AssertionError("must not pay for an invalid output")
    case = SimpleNamespace(question_id="q_abs", question_type="multi-session", question="q",
                           answer="unanswerable", should_abstain=True)
    trace = {"reader": {"status": "INVALID"}, "hypothesis": None}
    assert score_trace(NoJudge(), case, trace).correct is False
    report = summarize([case], {"q_abs": trace}, {"q_abs": True}, exposed_ids={"q_abs"})
    assert report["overall"]["correct"] == 0
    assert report["unanswerable"]["outcomes"] == {"invalid_output": 1}


def test_unjudged_prevents_headline_accuracy():
    case = SimpleNamespace(question_id="q", question_type="fact", question="q",
                           answer="a", should_abstain=False)
    trace = {"reader": {"status": "ANSWER"}, "hypothesis": "a"}
    report = summarize([case], {"q": trace}, {"q": None})
    assert report["overall"]["accuracy"] is None
    assert report["outside_current_screen"]["cases"] == 1


def test_balance_uses_decimal_and_requires_cny():
    assert balance_cny({"balance_infos": [{"currency": "CNY", "total_balance": "19.84"}]}) == Decimal("19.84")
    with pytest.raises(PilotStopped):
        balance_cny({"balance_infos": [{"currency": "USD", "total_balance": "20"}]})
