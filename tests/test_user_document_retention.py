from dataclasses import replace

import pytest

from dive_memory.user_document_retention import ExplicitUserDocumentRetention
from dive_memory.models import DOCUMENT_PREDICATE
from dive_memory.oracle.pipeline import PipelineConfig

BODY = ('The regional archive records several stages of this public project. '
        'Construction began in 2011 after a planning review. '
        'The building opened in 2013, while a later extension was finished in 2018. '
        'The text describes the project and does not describe the user personally.')


def collect(text, role='user', **kwargs):
    return ExplicitUserDocumentRetention(**kwargs).candidates(text, source_type=role,
        observed_at='2025-01-01T00:00:00Z', existing=[])


@pytest.mark.parametrize('intro', ['Please remember this document. ',
    'I want you to remember this case. ', 'I would like you to save the following material. ',
    'Keep in mind this report. ', '请记住这份材料。\n'])
def test_explicit_material_preserved_as_observation(intro):
    text = '  '+intro+BODY+'  '
    c, = collect(text)
    assert c.content == text.strip()
    assert c.evidence_state == 'OBSERVATION'
    assert c.structured_content['predicate'] == DOCUMENT_PREDICATE
    assert c.structured_content['role'] == 'user'
    assert 'value' not in c.structured_content
    a,b = c.structured_content['source_span']
    assert text[a:b] == c.content


@pytest.mark.parametrize('intro', ['I remember this material. ', "Don't remember this document. ",
    'Please do not save this report. ', 'The writer said "remember this document". ',
    'Can you summarize this document? ', 'If you remember this document, explain it. '])
def test_no_unrequested_document_retention(intro):
    assert collect(intro+BODY) == []


@pytest.mark.parametrize('role', ['assistant', 'tool', 'unknown'])
def test_only_user_source(role):
    assert collect('Remember this document. '+BODY, role=role) == []


def test_sensitive_floor_and_size_and_shape():
    assert collect('Remember this document. My password is hidden. '+BODY) == []
    assert collect('Remember this short note.') == []
    assert collect('Remember this document. '+BODY, max_chars=220) == []
    assert collect('Remember this document '+('ordinary words '*30)) == []


def test_existing_full_document_not_duplicated_and_base_retained():
    text='Remember this document. '+BODY
    c,=collect(text)
    class Base:
        def candidates(self, *args, **kwargs):
            return [c]
    assert collect(text, base=Base()) == [c]
    assert ExplicitUserDocumentRetention().candidates(text, source_type='user',
        observed_at=None, existing=[c]) == []


def test_short_existing_fact_does_not_hide_document():
    text='Remember this document. '+BODY
    c,=collect(text)
    short=replace(c, content='Remember this document.')
    added=ExplicitUserDocumentRetention().candidates(text, source_type='user',
        observed_at=None, existing=[short])
    assert len(added)==1 and added[0].content==text


def test_config_opt_in_and_bounds():
    assert PipelineConfig().explicit_user_documents is False
    assert PipelineConfig(explicit_user_documents=True).to_dict()['explicit_user_documents'] is True
    with pytest.raises(ValueError):
        PipelineConfig(explicit_user_documents=1)
    with pytest.raises(ValueError):
        ExplicitUserDocumentRetention(min_chars=300,max_chars=200)


def test_service_stores_document_without_personal_fact_and_honors_disable():
    from dive_memory.gate import GateAction, GateDecision
    from dive_memory.service import MemoryService
    def reject(text, **kwargs):
        return GateDecision(False,0,0,0,'ephemeral','test skip',GateAction.SKIP)
    service=MemoryService(gate=reject,episodic_policy=ExplicitUserDocumentRetention())
    service.ingest('u','Remember this document. '+BODY,source_type='user')
    memories=service.list_memories('u')
    assert len(memories)==1
    assert memories[0].structured_content['predicate']==DOCUMENT_PREDICATE
    assert memories[0].evidence_state.value=='OBSERVATION'
    service.set_memory_enabled('disabled',False)
    result=service.ingest('disabled','Remember this document. '+BODY,source_type='user')
    assert result['memory_disabled'] and not service.list_memories('disabled')
