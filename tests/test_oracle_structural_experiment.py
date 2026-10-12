from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_structural_experiment import shortlist, choose_paid, ARMS
from dive_memory.oracle.pipeline import PipelineConfig, run_case


def metric(packed, complete=20, temporal=10):
    return {"packed": packed, "complete_positive_cases": complete,
            "by_type_packed": {"temporal-reasoning": temporal}}


def test_offline_selector_guards_type_regressions_and_prefers_simpler_ties():
    assert shortlist({"baseline": metric(30), "episodes": metric(31)}) == []
    assert shortlist({"baseline": metric(30), "episodes": metric(40, temporal=7)}) == []
    assert shortlist({"baseline": metric(30), "episodes": metric(32),
                      "semantic": metric(32), "roomy": metric(32)}) == ["episodes", "semantic"]


def test_paid_selector_does_not_promote_refusal_only_gain():
    def report(p, n):
        return {"answerable": {"correct": p}, "unanswerable": {"correct": n}}
    assert choose_paid(report(30, 23), {"episodes": report(30, 30)}) is None
    assert choose_paid(report(30, 23), {"episodes": report(40, 21)}) is None
    assert choose_paid(report(30, 23), {"episodes": report(32, 22)}) == "episodes"


def test_baseline_defaults_do_not_enable_structural_candidates():
    c = PipelineConfig()
    assert not c.episodic_retention and not c.evidence_recovery and not c.semantic_retrieval_only
    assert ARMS["roomy"].limit == 10
    assert ARMS["roomy"].token_budget == 3000
