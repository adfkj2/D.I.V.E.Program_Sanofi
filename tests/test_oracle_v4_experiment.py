from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_v4_experiment import select_candidate


def summary(positive, negative):
    return {"answerable": {"correct": positive}, "unanswerable": {"correct": negative}}


def test_selection_requires_answerable_gain_and_protects_abstention():
    baseline = summary(30, 23)
    assert select_candidate(baseline, {"v4": summary(31, 30)})["selected"] is None
    assert select_candidate(baseline, {"v4": summary(40, 21)})["selected"] is None
    assert select_candidate(baseline, {"v4": summary(32, 22)})["selected"] == "v4"


def test_tie_prefers_prompt_only_arm():
    report = select_candidate(summary(30, 23), {"v4": summary(33, 25), "v4-coordinates": summary(33, 25)})
    assert report["selected"] == "v4"
