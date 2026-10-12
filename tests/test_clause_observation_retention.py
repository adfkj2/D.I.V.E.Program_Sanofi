"""Specification for opt-in mixed-speech-act observation anchors.

Synthetic examples only: no benchmark IDs, gold answers or questions.
"""
import pytest

from dive_memory.gate import GateAction, GateDecision
from dive_memory.narrative_retention import NarrativeRetentionPolicy


def reject(text, **kwargs):
    return GateDecision(False, 0, 0, 0, "ephemeral", "skip", GateAction.SKIP)


def retain(text, **kwargs):
    return NarrativeRetentionPolicy(reject, clause_anchors=True).candidates(
        text, source_type=kwargs.get("source_type", "user"), observed_at="2026-01-03",
        existing=[])


@pytest.mark.parametrize("text,fragment", [
    ("I plan to buy a replacement, because I lost my backpack three days ago.", "lost my backpack"),
    ("I repaired the desk last week, so I hope it lasts longer.", "repaired the desk"),
    ("I redeemed 350 points on Friday, and I might return next month.", "redeemed 350 points"),
    ("We accumulated four vouchers last month, but we want to save the next batch.", "four vouchers"),
    ("I signed up for the workshop yesterday. Could you suggest supplies?", "signed up"),
])
def test_independent_factual_clause_survives_intention(text, fragment):
    candidates = retain(text)
    assert candidates and any(fragment in c.content for c in candidates)
    for c in candidates:
        a, b = c.structured_content["source_span"]
        assert text[a:b] == c.content
        assert c.evidence_state == "OBSERVATION"
        assert c.structured_content["assertion"] == "reported_context"
        assert c.structured_content["context_may_include_questions_or_hypotheses"]


@pytest.mark.parametrize("text", [
    "If I bought four tickets yesterday, how much would that cost?",
    "Imagine I repaired the desk last week, and I redeemed 350 points.",
    "For this fictional story, I collected six stones yesterday.",
    "I hope I earned three badges yesterday.",
    "I plan to buy four tickets tomorrow.",
    "Did I redeem 350 points on Friday?",
    "Can you tell me if I repaired the desk last week?",
    "I want you to say I purchased two books yesterday.",
    "The example sentence is: I purchased two books yesterday.",
    "I might have received five badges last month.",
    "I repaired the desk yesterday. My password is secret.",
    "The train arrived three hours late.",
])
def test_no_assertion_from_hypothetical_request_or_untrusted_material(text):
    assert not retain(text)


def test_role_floor_and_opt_in_and_boundedness():
    text = "I replaced two valves yesterday, so I hope it works."
    assert not retain(text, source_type="assistant")
    assert not retain(text, source_type="untrusted_web")
    assert not NarrativeRetentionPolicy(reject).candidates(text, source_type="user", observed_at=None, existing=[])
    p = NarrativeRetentionPolicy(reject, clause_anchors=True, max_window_chars=80, max_items=1)
    cs = p.candidates(text * 3, source_type="user", observed_at=None, existing=[])
    assert len(cs) <= 1 and all(len(c.content) <= 80 for c in cs)


def test_clause_observations_obey_isolation_disable_and_source_forgetting():
    from dive_memory.service import MemoryService
    service = MemoryService(gate=reject, episodic_policy=NarrativeRetentionPolicy(reject,clause_anchors=True))
    text = "I repaired the desk last week, so I hope it lasts longer."
    service.set_memory_enabled("disabled",False)
    assert service.ingest("disabled",text,source_type="user")["memory_disabled"]
    assert not service.list_memories("disabled")
    result = service.ingest("owner",text,source_type="user")
    assert result["memory_ids"]
    assert not service.retrieve("other-user","desk").items
    service.forget_event(result["event_id"])
    assert not service.retrieve("owner","desk").items
