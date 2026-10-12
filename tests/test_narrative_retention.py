import pytest
from dive_memory.narrative_retention import NarrativeRetentionPolicy
from dive_memory.gate import GateDecision, GateAction
from dive_memory.service import MemoryService


def gate(text, **kwargs):
    return GateDecision(True, .7, .8, .6, "medium_term", "test admitted", GateAction.WRITE)


def test_preserves_adjacent_duration_and_exact_source_span():
    text = "I received the result of my permit application. Over eight months of waiting was exhausting. What now?"
    cs = NarrativeRetentionPolicy(gate).candidates(text, source_type="user", observed_at=None, existing=[])
    assert len(cs) == 1 and "Over eight months" in cs[0].content
    a, b = cs[0].structured_content["source_span"]
    assert cs[0].content == text[a:b]
    assert cs[0].evidence_state == "OBSERVATION"


@pytest.mark.parametrize("text,role", [
    ("I received two grants. My password is secret.", "user"),
    ("I received two grants.", "assistant"),
    ("Could I have two grants?", "user"),
    ("Imagine I received two grants.", "user"),
    ("The weather is nice today.", "user"),
])
def test_not_full_history_retention(text, role):
    assert not NarrativeRetentionPolicy(gate).candidates(text, source_type=role, observed_at=None, existing=[])


def test_windows_bounded_and_user_control_preserved():
    p = NarrativeRetentionPolicy(gate, max_items=1, max_window_chars=100, neighbor_sentences=1)
    text = "I bought three books yesterday. " * 20
    cs = p.candidates(text, source_type="user", observed_at=None, existing=[])
    assert len(cs) == 1 and len(cs[0].content) <= 100
    s = MemoryService(gate=gate, episodic_policy=p)
    s.set_memory_enabled("u", False)
    assert s.ingest("u", text, source_type="user")["memory_disabled"]
    assert not s.list_memories("u")


def test_rejected_generic_personal_sentence_does_not_bypass_gate():
    def reject(text, **kwargs):
        return GateDecision(False, 0, 0, 0, "ephemeral", "skip", GateAction.SKIP)
    p = NarrativeRetentionPolicy(reject)
    assert not p.candidates("I feel rather cheerful about everything.", source_type="user", observed_at=None, existing=[])
