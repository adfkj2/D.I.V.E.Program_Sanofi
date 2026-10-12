from dataclasses import replace
import pytest
from dive_memory.contextual_fact_retention import ContextualFactRetention, has_contextual_fact
from dive_memory.narrative_retention import NarrativeRetentionPolicy
from dive_memory.oracle.pipeline import PipelineConfig
from test_reported_detail_retention import reject


@pytest.mark.parametrize('text',[
    'I want to visit my friends Aria and Ben, who I met at a workshop three weeks ago, any travel tips?',
    'Can you help with training? I recently did a 10K in September.',
    'My parents are growing older - my mother is 63 and my father is 65.',
    'I am hoping to beat my best score of 870 this season.',
    'By the way, my sister is using the same calendar app as me now.',
    'I need to finish before dinner, which is usually at 7:15 pm.',
    'I plan to play cards on Friday, like we do every other week.',
    'Our basketball team, the Cedar Owls, is leading with a 7-3 record.',
    'Any routes nearby, considering I like to wake up at 6:20 on Sundays?',
    'I want to organize my old boots in a shoe rack in the closet.',
    'I am thinking of visiting my cousin Eva in Lisbon soon.',
    'Can you suggest exercise, considering I used to play squash competitively in college?',
    'My clip about the dog has 234 views, any ideas for more clips?',
])
def test_context_preserves_assertions_and_all_their_qualifiers(text):
    rows=ContextualFactRetention().candidates(text,source_type='user',observed_at='2025-03-01',existing=[])
    assert rows and rows[0].content==text
    for c in rows:
        assert c.evidence_state=='OBSERVATION' and 'value' not in c.structured_content
        assert text[slice(*c.structured_content['source_span'])]==c.content
        assert c.structured_content['role']=='user'


@pytest.mark.parametrize('text',[
    'I am going to buy three guitars tomorrow.', 'I hope I bought three guitars.',
    'If my brother is 39, which present should I buy?', 'Imagine I met Ted in June.',
    'My brother is going to turn 40 next year.', 'I have to buy five lamps.',
    'My cousin is probably 42.', 'I wish my team is winning with a 9-0 record.',
    'What is the address of my doctor?', 'Could you recommend four keyboards?'])
def test_pure_plan_hypothesis_question_or_uncertainty_cannot_anchor(text):
    assert not has_contextual_fact(text)


def test_roles_bounds_duplicates_and_base_are_preserved():
    text='Our tennis team, the Pine Owls, is playing with a 4-1 record.'
    policy=ContextualFactRetention()
    accepted=policy.candidates(text,source_type='user',observed_at=None,existing=[])
    assert accepted
    assert not policy.candidates(text,source_type='assistant',observed_at=None,existing=[])
    assert not policy.candidates(text,source_type='user',observed_at=None,existing=accepted)
    assert not replace(policy,max_turn_chars=10).candidates(text,source_type='user',observed_at=None,existing=[])
    base=NarrativeRetentionPolicy(reject,reported_detail_anchors=True,reported_detail_assertions_only=True,precise_detail_terms=True)
    old=base.candidates('I bought four books yesterday.',source_type='user',observed_at=None,existing=[])
    wrapped=ContextualFactRetention(base).candidates('I bought four books yesterday.',source_type='user',observed_at=None,existing=[])
    assert wrapped==old


def test_config_default_off_and_type_checked():
    assert not PipelineConfig().contextual_fact_anchors
    assert PipelineConfig(contextual_fact_anchors=True).to_dict()['contextual_fact_anchors']
    with pytest.raises(ValueError):PipelineConfig(contextual_fact_anchors='yes')
