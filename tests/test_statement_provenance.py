"""Synthetic provenance, cutoff, immutable memory and exact replay invariants."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from dive_memory.context import pack_context
from dive_memory.models import Memory, EvidenceState, RetrievalItem
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.oracle.pipeline import PipelineConfig, run_case
from dive_memory.oracle.reader import DeterministicReader, render_reader_context
from dive_memory.oracle.statement_provenance import attach_statement_provenance
from dive_memory.token_budget import ApproximateTokenCounter
from test_oracle_pipeline import _case, GOLD_SENTINEL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'audit_tools'))
from restore_statement_request import restore_visible_request


def item(mid='m1', refs=('e1',), valid='2025-01-10T09:00:00+00:00'):
    text = 'I received my camera today. The warranty starts next month.'
    return RetrievalItem(Memory(mid, 'ns', 'semantic_fact', EvidenceState.FACT, text,
        {'subject': 'user', 'predicate': 'statement', 'value': text}, valid_from=valid,
        observed_at='2025-02-15T09:00:00+00:00'), 1.0, ['bm25'], list(refs))


SOURCES = {
    'e1': {'role': 'user', 'statement_at': '2025-02-15T09:00:00+00:00'},
    'e2': {'role': 'assistant', 'statement_at': '2025-03-20T11:00:00+00:00'},
}


def render(items):
    return render_evidence_block(build_evidence_views(items, question='When did I receive my camera?'))


def test_message_clock_does_not_rewrite_event_clock_or_original_memory():
    original = item()
    saved = deepcopy(original)
    annotated = attach_statement_provenance([original], SOURCES)
    assert original == saved
    assert annotated[0].memory.content == original.memory.content
    assert annotated[0].memory.valid_from == original.memory.valid_from
    assert annotated[0].memory.observed_at == original.memory.observed_at
    text = render(annotated)
    assert 'valid=2025-01-10' in text
    assert 'statement_at=2025-02-15' in text
    assert 'not necessarily the events' in text
    assert 'plan/completion' in text
    assert original.memory.content in text


def test_multiple_sources_keep_order_roles_and_distinct_dates():
    annotated = attach_statement_provenance([item(refs=('e2', 'e1'))], SOURCES)
    view = build_evidence_views(annotated, question='camera')[0]
    assert view.role == 'mixed'
    assert [s['source_ref'] for s in view.statement_sources] == ['e2', 'e1']
    assert '1:assistant@statement_at=2025-03-20' in render(annotated)
    assert '2:user@statement_at=2025-02-15' in render(annotated)


@pytest.mark.parametrize('sources,expected_role', [({}, 'unknown'), (SOURCES, 'mixed')])
def test_missing_provenance_is_explicit_not_imputed(sources, expected_role):
    annotated = attach_statement_provenance([item(refs=('e1', 'missing'))], sources)
    view = build_evidence_views(annotated, question='camera')[0]
    assert view.role == expected_role
    assert view.statement_sources[-1] == {'source_ref': 'missing', 'role': 'unknown', 'statement_at': None}
    assert '2:unknown@statement_at=?' in render(annotated)


def test_only_allowlisted_fields_can_enter_provenance():
    source = {'e1': {**SOURCES['e1'], 'answer': GOLD_SENTINEL, 'has_answer': True,
                     'question_type': GOLD_SENTINEL, 'source_ref': 'wrong'}}
    annotated = attach_statement_provenance([item()], source)
    assert annotated[0].memory.structured_content['_reader_statement_sources'] == [
        {'source_ref': 'e1', **SOURCES['e1']}]
    assert GOLD_SENTINEL not in render(annotated)


def test_canonical_merge_keeps_sources_without_mutating_inputs():
    annotated = attach_statement_provenance([item(), item('m2', ('e2',))], SOURCES)
    saved = deepcopy(annotated)
    packed = pack_context(annotated, token_budget=2000, render_for_budget=render)
    assert len(packed.items) == 1
    view = build_evidence_views(packed.visible_items, question='camera')[0]
    assert view.source_refs == ('e1', 'e2')
    assert view.role == 'mixed' and len(view.statement_sources) == 2
    assert annotated == saved


def test_merge_with_unannotated_source_keeps_missing_marker():
    annotated = attach_statement_provenance([item()], SOURCES)
    packed = pack_context([*annotated, item('m2', ('missing',))], token_budget=2000,
                          render_for_budget=render)
    assert '2:unknown@statement_at=?' in packed.text


def test_annotation_and_preamble_count_toward_same_budget():
    annotated = attach_statement_provenance([item()], SOURCES)
    counter = ApproximateTokenCounter()
    size = counter.count(render(annotated))
    assert size > counter.count(render([item()]))
    exact = pack_context(annotated, size, render_for_budget=render)
    assert len(exact.items) == 1 and exact.estimated_tokens == size
    assert not pack_context(annotated, size - 1, render_for_budget=render).items
    assert render([]) == ''


@pytest.mark.parametrize('valid', [None, '2025-01-10T09:00:00+00:00'])
def test_replay_preserves_observation_and_source_clocks(valid):
    annotated = attach_statement_provenance([item(valid=valid)], SOURCES)
    question, at = 'When did I receive my camera?', '2025-05-01T09:00:00+00:00'
    views = build_evidence_views(annotated, question=question, question_date=at)
    trace = {'namespace': 'ns', 'as_of': at, 'evidence': {'views': [v.to_dict() for v in views]},
             'reader_context_text': render_evidence_block(views), 'packing': {'omitted': 0}}
    restored = restore_visible_request(question, trace)
    assert render_reader_context(restored) == trace['reader_context_text']
    changed = deepcopy(trace)
    changed['evidence']['views'][0]['statement_sources'][0]['statement_at'] = '2020-01-01'
    with pytest.raises(ValueError, match='differs'):
        restore_visible_request(question, changed)


def test_end_to_end_metadata_uses_only_ingested_turns_and_defaults_stay_off():
    case = _case(answer=GOLD_SENTINEL, sessions=[
        ('s1', '2024-05-01T10:00:00Z', [{'role': 'user',
            'content': 'Please remember that my golden retriever is named Comet.', 'has_answer': True}]),
        ('future', '2025-01-01T10:00:00Z', [{'role': 'user',
            'content': 'Please remember that my golden retriever is named NOVAFUTURE.'}]),
    ])
    config = PipelineConfig(reader_policy_name='indexed_evidence', token_budget=6000)
    old = run_case(case, reader=DeterministicReader(), config=config).record
    new = run_case(case, reader=DeterministicReader(), config=replace(config, statement_provenance=True)).record
    assert not config.statement_provenance
    assert not config.to_dict()['statement_provenance']
    assert old['formation'] == new['formation']
    assert old['retrieval']['returned_source_refs'] == new['retrieval']['returned_source_refs']
    assert 'statement_at=' not in old['reader_context_text']
    assert new['evidence']['views']
    for view in new['evidence']['views']:
        assert view['role'] == 'user'
        assert all(s['statement_at'] == '2024-05-01T10:00:00+00:00' for s in view['statement_sources'])
    assert GOLD_SENTINEL not in new['reader_context_text']
    assert 'NOVAFUTURE' not in new['reader_context_text']
    assert render_reader_context(restore_visible_request(case.question, new)) == new['reader_context_text']
    assert render_reader_context(restore_visible_request(case.question, old)) == old['reader_context_text']
