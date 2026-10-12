import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_reasoning_experiment import eligible, THINKING, EXTRA, CONFIG


def report(positive, negative):
    return {"answerable": {"correct": positive}, "unanswerable": {"correct": negative}}


def test_gate_requires_answerable_gain_without_negative_regression():
    base = report(37, 28)
    assert eligible(base, report(40, 28))
    assert not eligible(base, report(39, 30))
    assert not eligible(base, report(43, 27))


def test_thinking_and_judge_are_separate_without_evidence_expansion():
    assert THINKING == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    assert EXTRA == {"thinking": {"type": "disabled"}}
    assert CONFIG.token_budget == 6000 and CONFIG.limit == 30
