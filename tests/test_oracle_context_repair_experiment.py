from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_context_repair_experiment import select, ARMS
from dive_memory.oracle.pipeline import PipelineConfig


def metric(packed=10,complete=5,chars=1000,assistant=3):
    return {"packed":packed,"complete_positive_cases":complete,"visible_chars":chars,
            "by_type_packed":{"single-session-assistant":assistant}}


def test_free_selector_never_calls_visible_length_an_accuracy_gain():
    assert select({"baseline":metric(),"documents":metric(chars=1001)}) is None
    assert select({"baseline":metric(),"documents":metric(chars=1400)}) == "documents"
    assert select({"baseline":metric(),"combined":metric(11,6,2000,assistant=2)}) is None
    assert select({"baseline":metric(),"documents":metric(chars=1400),
                   "combined":metric(11,6,1300)}) == "combined"


def test_new_controls_are_explicit_and_fingerprinted():
    c = ARMS["combined"]
    assert c.to_dict()["narrative_clause_anchors"]
    assert c.to_dict()["expand_document_spans"]
    assert c.to_dict()["controlled_document_limit"] == 4
    assert c.to_dict()["controlled_document_char_budget"] == 2400
    assert c.to_dict()["controlled_document_token_budget"] == 2400
    assert not PipelineConfig().narrative_clause_anchors
    assert not PipelineConfig().expand_document_spans
    with pytest.raises(ValueError):
        PipelineConfig(narrative_clause_anchors=True)
    with pytest.raises(ValueError):
        PipelineConfig(controlled_document_char_budget=0)
    with pytest.raises(ValueError):
        PipelineConfig(controlled_document_limit=-1)
