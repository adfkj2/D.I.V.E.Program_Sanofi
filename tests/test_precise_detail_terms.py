"""Actual modal verbs and unrelated prefixes must not be conflated."""
import pytest
from dive_memory.narrative_retention import NarrativeRetentionPolicy, _has_reported_detail
from dive_memory.oracle.pipeline import PipelineConfig
from test_reported_detail_retention import reject


@pytest.mark.parametrize('text', [
    'My planter is 40 centimeters wide.',
    'We participated in the PlanetWatch survey yesterday.',
    'My wishbone is 12 centimeters long.',
    'I installed two lights in the hopelessly dark hallway.',
])
def test_unrelated_prefix_is_not_a_modal_verb(text):
    assert not _has_reported_detail(text, assertions_only=True)
    assert _has_reported_detail(text, assertions_only=True, precise_terms=True)
    policy = NarrativeRetentionPolicy(reject, reported_detail_anchors=True,
        reported_detail_assertions_only=True, precise_detail_terms=True)
    result = policy.candidates(text, source_type='user', observed_at=None, existing=[])
    assert result and result[0].content == text
    assert text[slice(*result[0].structured_content['source_span'])] == text
    assert result[0].evidence_state == 'OBSERVATION'


@pytest.mark.parametrize('text', [
    'I am planning to buy three lamps.',
    'I planned to buy three lamps.',
    'My sister plans to buy three lamps.',
    'I wanted three lamps.',
    'I am hoping to get three lamps.',
    'I wished for three lamps.',
    'I intend to install three lamps.',
    'I intended to install three lamps.',
    'I might install three lamps.',
    'If I installed three lamps, the room would be brighter.',
    'Can you say I installed three lamps?',
    'I am going to install three lamps.',
])
def test_real_intention_question_and_hypothesis_remain_excluded(text):
    assert not _has_reported_detail(text, assertions_only=True, precise_terms=True)


def test_role_sensitive_limits_and_opt_in_remain():
    text = 'My planter is 40 centimeters wide.'
    policy = NarrativeRetentionPolicy(reject, reported_detail_anchors=True,
        reported_detail_assertions_only=True, precise_detail_terms=True)
    for content, role in [(text, 'assistant'), (text + ' My password is secret.', 'user'),
                          (text * 300, 'user')]:
        assert not policy.candidates(content, source_type=role, observed_at=None, existing=[])
    assert not PipelineConfig().precise_detail_terms
    with pytest.raises(ValueError):
        PipelineConfig(precise_detail_terms=True)
    with pytest.raises(ValueError):
        NarrativeRetentionPolicy(reject, precise_detail_terms=True)
    assert PipelineConfig(narrative_retention=True, reported_detail_anchors=True,
                          precise_detail_terms=True).to_dict()['precise_detail_terms']
