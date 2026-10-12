from dataclasses import replace
import inspect
from pathlib import Path
import sys

import pytest

from dive_memory.models import Memory, EvidenceState, RetrievalItem, DOCUMENT_PREDICATE
from dive_memory.oracle.controlled_documents import _controlled_candidates, _controlled_pack
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.user_document_retention import VERSION


def item(name, *, document=True, role='assistant', explicit=False):
    structured={'predicate':DOCUMENT_PREDICATE if document else 'statement','role':role}
    if explicit:
        structured['retention_policy']=VERSION
    return RetrievalItem(Memory(name,'u','episode',EvidenceState.OBSERVATION,
        'The archive records that the northern building opened in 2012.', structured), 1,
        source_refs=['source-'+name])


def test_opt_in_keeps_all_facts_and_uses_existing_document_capacity():
    fact,a,b,u=item('fact',document=False),item('a'),item('b'),item('u',role='user',explicit=True)
    base=[fact,a,b,u]
    supplement=[b,u,a]
    _,old,_=_controlled_candidates(base,supplement,limit=10)
    assert [x.memory.id for x in old]==['a','b']
    facts,docs,audit=_controlled_candidates(base,supplement,limit=10,prefer_explicit_user_documents=True)
    assert facts==[fact] and [x.memory.id for x in docs]==['u','a']
    assert audit['max_document_candidates']==2


@pytest.mark.parametrize('role,explicit,in_supplement', [('assistant',True,True),('user',False,True),('user',True,False)])
def test_only_explicit_user_document_retrieval_hits_qualify(role,explicit,in_supplement):
    a,b,u=item('a'),item('b'),item('u',role=role,explicit=explicit)
    _,docs,_=_controlled_candidates([a,b,u],[b,*([u] if in_supplement else []),a],
        prefer_explicit_user_documents=True)
    assert [x.memory.id for x in docs]==['a','b']


def test_at_most_one_explicit_document_gets_priority_and_caps_survive():
    a,u,v=item('a'),item('u',role='user',explicit=True),item('v',role='user',explicit=True)
    _,docs,_=_controlled_candidates([a,u,v],[u,v,a],prefer_explicit_user_documents=True)
    assert [x.memory.id for x in docs]==['u','a']
    _,docs,_=_controlled_candidates([a,u,v],[u,v,a],max_documents=0,prefer_explicit_user_documents=True)
    assert docs==[]
    fact=item('fact',document=False)
    facts,docs,_=_controlled_candidates([fact,a,u],[u,a],limit=1,prefer_explicit_user_documents=True)
    assert facts==[fact] and docs==[]


def test_priority_documents_still_obey_packing_budget():
    fact=item('f',document=False)
    doc=item('d',role='user',explicit=True)
    pack,audit=_controlled_pack([fact],[doc],question='When did it open?',question_date='2025-01-01',
        current_only=False,document_spans={},token_budget=300,document_token_budget=1)
    assert [i.memory.id for i in pack.items]==['f']
    assert audit['documents_packed']==0


def test_config_requires_explicit_retention_and_controlled_lane():
    assert not PipelineConfig().prefer_explicit_user_documents
    with pytest.raises(ValueError):
        PipelineConfig(prefer_explicit_user_documents=True)
    with pytest.raises(ValueError):
        PipelineConfig(explicit_user_documents=True,prefer_explicit_user_documents=True)


def test_r33_keeps_verified_reader_cost_and_scoring_driver():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
    import oracle_user_document_experiment as old
    import oracle_user_document_priority_experiment as new
    for name in ('make_reader','audit_reader_cache','preflight','run_stage','prepare_full'):
        assert inspect.getsource(getattr(new,name))==inspect.getsource(getattr(old,name))
    assert new.make_reader(None).spec==old.make_reader(None).spec
    assert new.CANDIDATE_CONFIG.prefer_explicit_user_documents
    assert new.OUTPUT != old.OUTPUT
