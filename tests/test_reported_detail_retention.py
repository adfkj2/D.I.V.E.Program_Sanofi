"""Source retention tests use synthetic text, no benchmark-specific rules."""
import pytest
from dive_memory.gate import GateAction, GateDecision
from dive_memory.narrative_retention import NarrativeRetentionPolicy
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.service import MemoryService


def reject(text, **kwargs):
    return GateDecision(False, 0, 0, 0, "ephemeral", "skip", GateAction.SKIP)


def retain(text, **kwargs):
    return NarrativeRetentionPolicy(reject, reported_detail_anchors=True).candidates(
        text, source_type=kwargs.get("role", "user"), observed_at="2026-01-03", existing=[])


@pytest.mark.parametrize("text", [
    "My brother is 37 and my sister is 41.",
    "I usually go to the pool at 7 pm.",
    "I repaired three shelves yesterday, so I hope they last.",
    "I installed four lights last month, can you suggest bulbs?",
    "I plan to buy a replacement since I lost my bag two days ago.",
    "By the way, I'm now at 430 subscribers.",
    "We participated in the Cedar Festival yesterday.",
    "I've replaced two taps this week.",
])
def test_specific_reported_details_keep_verbatim_context(text):
    cs = retain(text)
    assert cs
    for c in cs:
        a, b = c.structured_content['source_span']
        assert c.content == text[a:b]
        assert c.content == text
        assert c.evidence_state == 'OBSERVATION'
        assert c.structured_content['role'] == 'user'
        assert c.structured_content['assertion'] == 'reported_context'


@pytest.mark.parametrize("text", [
    "Did I install four lights last month?",
    "I installed four lights last month?",
    "If my brother is 37, how old is my sister?",
    "Imagine I repaired three shelves yesterday.",
    "Can you say I installed four lights yesterday?",
    "I hope my brother is 37.",
    "I might have repaired three shelves yesterday.",
    "The fictional example is: My brother is 37.",
    "I plan to install four lights tomorrow.",
    "The weather is pleasant today.",
    "I feel cheerful about everything.",
    "My sister is 41. My password is secret.",
])
def test_no_question_hypothesis_request_or_generic_filler(text):
    assert not retain(text)


def test_future_qualifier_is_never_rewritten_into_completed_fact():
    text = 'My brother is going to turn 37 next year.'
    candidate = retain(text)[0]
    assert candidate.content == text
    assert candidate.evidence_state == 'OBSERVATION'


def test_role_limits_default_and_memory_controls():
    text = 'My brother is 37 and my sister is 41.'
    assert not retain(text, role='assistant')
    assert not retain(text, role='untrusted_web')
    assert not NarrativeRetentionPolicy(reject).candidates(text, source_type='user', observed_at=None, existing=[])
    p = NarrativeRetentionPolicy(reject, reported_detail_anchors=True, max_items=1, max_window_chars=60)
    cs = p.candidates(text * 4, source_type='user', observed_at=None, existing=[])
    assert len(cs) <= 1 and all(len(c.content) <= 60 for c in cs)
    service = MemoryService(gate=reject, episodic_policy=p)
    service.set_memory_enabled('disabled', False)
    assert service.ingest('disabled', text, source_type='user')['memory_disabled']
    assert not service.list_memories('disabled')
    result = service.ingest('owner', text, source_type='user')
    assert result['memory_ids']
    assert not service.retrieve('other', 'brother').items
    service.forget_event(result['event_id'])
    assert not service.retrieve('owner', 'brother').items


def test_config_is_opt_in_and_fingerprinted():
    assert not PipelineConfig().reported_detail_anchors
    with pytest.raises(ValueError):
        PipelineConfig(reported_detail_anchors=True)
    assert PipelineConfig(narrative_retention=True, reported_detail_anchors=True).to_dict()['reported_detail_anchors']
