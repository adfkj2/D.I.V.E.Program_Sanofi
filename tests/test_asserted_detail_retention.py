"""R26: source-preserving precision constraint on optional new anchors."""
import pytest
from dive_memory.narrative_retention import NarrativeRetentionPolicy
from dive_memory.oracle.pipeline import PipelineConfig
from test_reported_detail_retention import reject


def candidates(text, *, strict=True):
    return NarrativeRetentionPolicy(reject, reported_detail_anchors=True,
        reported_detail_assertions_only=strict).candidates(text, source_type='user', observed_at=None, existing=[])


@pytest.mark.parametrize('text', [
    "I'm definitely going to buy two notebooks.",
    "I am about to install three shelves.",
    "My brother is going to turn 37 next year.",
    "I am scheduled to run 8 miles tomorrow.",
    "We are hoping to earn 4 badges.",
])
def test_future_or_wish_cannot_independently_supply_new_anchor(text):
    assert candidates(text, strict=False)
    assert not candidates(text)


@pytest.mark.parametrize('text', [
    "I own four notebooks, but I am going to buy two more.",
    "My brother Will is 37 and my sister is 41.",
    "I was going to Berlin yesterday.",
    "I repaired three shelves last week, so I am hoping they last longer.",
    "My rent is now set to 850 dollars.",
    "I missed three trains due to heavy rain.",
])
def test_asserted_detail_keeps_whole_original_context_and_qualifiers(text):
    result = candidates(text)
    assert result and result[0].content == text
    assert result[0].evidence_state == 'OBSERVATION'
    assert text[slice(*result[0].structured_content['source_span'])] == text


def test_constraint_is_opt_in_and_requires_detail_path():
    assert not PipelineConfig().reported_detail_assertions_only
    with pytest.raises(ValueError):
        PipelineConfig(reported_detail_assertions_only=True)
    with pytest.raises(ValueError):
        NarrativeRetentionPolicy(reject, reported_detail_assertions_only=True)
    config = PipelineConfig(narrative_retention=True, reported_detail_anchors=True,
        reported_detail_assertions_only=True)
    assert config.to_dict()['reported_detail_assertions_only']


def test_existing_wish_and_modal_rejections_remain():
    for text in ('I am wishing for three new tools.', 'I will buy three new tools.'):
        assert not candidates(text, strict=False)
        assert not candidates(text)
