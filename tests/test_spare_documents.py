"""Synthetic invariants for preserving evidence during spare-budget expansion."""
from copy import deepcopy
from dataclasses import replace

import pytest

from dive_memory.models import DOCUMENT_PREDICATE, EvidenceState, Memory, RetrievalItem
from dive_memory.oracle.controlled_documents import _controlled_pack
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.spare_documents import extend_documents
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.token_budget import ApproximateTokenCounter

DATE = '2025-06-01T12:00:00+00:00'


def item(mid, text, document=True):
    return RetrievalItem(Memory(mid, 'synthetic', 'document' if document else 'semantic_fact',
        EvidenceState.OBSERVATION, text,
        {'subject': 'assistant' if document else 'user',
         'predicate': DOCUMENT_PREDICATE if document else 'statement', 'value': text},
        valid_from=DATE), 1.0, ['document_chunk' if document else 'bm25'], ['event-' + mid])


def fixture():
    text = ''.join(f'Workshop section {i}: the blue instrument was inspected before shipment.\n'
                   for i in range(75))
    docs = [item('doc1', text), item('doc2', text.replace('blue', 'amber'))]
    facts = [item('fact1', 'I ordered the instrument yesterday.', False)]
    spans = {d.memory.id: (550, 1000) for d in docs}
    return facts, docs, spans


def prepare(facts, docs, spans, *, budget=2000, doc_budget=220):
    return _controlled_pack(facts, docs, question='workshop instrument', question_date=DATE,
        current_only=True, document_spans=spans, token_budget=budget,
        document_token_budget=doc_budget, max_documents=2, document_char_budget=500)[0]


def extend(base, docs, spans, *, budget=2000, doc_budget=1600, chars=3000):
    return extend_documents(base, docs, question='workshop instrument', question_date=DATE,
        current_only=True, document_spans=spans, token_budget=budget, max_documents=2,
        document_token_budget=doc_budget, max_chars=chars, base_char_budget=500)


def assert_preserved(base, result):
    for index, old in enumerate(base.visible_items):
        new = result.visible_items[index]
        assert new.memory.id == old.memory.id
        assert new.source_refs == old.source_refs
        assert old.memory.content in new.memory.content


def test_extension_adds_originally_excluded_document_without_displacing_baseline():
    facts, docs, spans = fixture()
    base = prepare(facts, docs, spans)
    assert len(base.items) == 2  # fact plus one document under the old quota
    saved = deepcopy((base, facts, docs))
    result, audit = extend(base, docs, spans)
    assert (base, facts, docs) == saved
    assert_preserved(base, result)
    assert result.visible_items[0] == base.visible_items[0]
    assert len(result.items) == 3
    assert audit['added_document_ids'] == ['doc2']
    assert audit['expanded_document_count'] > 0 and audit['new_visible_characters'] > 0
    assert audit['document_context_tokens'] <= 1600
    assert result.estimated_tokens <= 2000
    for change in result.decisions:
        if change['decision'] == 'EXPAND':
            full = next(d.memory.content for d in docs if d.memory.id == change['memory_id'])
            visible = next(d.memory.content for d in result.visible_items if d.memory.id == change['memory_id'])
            assert full[change['visible_start']:change['visible_end']] == visible
            assert change['visible_start'] <= change['previous_start'] < change['previous_end'] <= change['visible_end']


def test_no_spare_budget_leaves_visible_evidence_exactly_unchanged():
    facts, docs, spans = fixture()
    base = prepare(facts, docs, spans)
    result, audit = extend(base, docs, spans, budget=base.estimated_tokens)
    assert result.text == base.text
    assert result.visible_items == base.visible_items
    assert result.omitted == base.omitted
    assert audit['new_visible_characters'] == 0


def test_small_spare_budget_never_evicts_an_existing_fragment():
    facts, docs, spans = fixture()
    base = prepare(facts, docs, spans, doc_budget=450)
    assert len(base.items) == 3
    result, audit = extend(base, docs, spans, budget=base.estimated_tokens + 200)
    assert_preserved(base, result)
    assert result.estimated_tokens <= base.estimated_tokens + 200
    assert audit['new_visible_characters'] >= 0


def test_full_document_and_zero_document_cases_are_noops():
    fact = item('fact', 'The instrument arrived yesterday.', False)
    for docs in [[], [item('short', 'Workshop instrument instructions: keep dry.')]]:
        base = prepare([fact], docs, {})
        result, audit = extend(base, docs, {})
        assert result.text == base.text
        assert audit['new_visible_characters'] == 0


def test_expansion_uses_recorded_offsets_with_repeated_text():
    facts, docs, spans = fixture()
    repeated = 'Identical chapter body. ' * 250
    docs = [item('repeated', repeated)]
    spans = {'repeated': (1150, 1610)}
    base = prepare(facts, docs, spans)
    result, _ = extend(base, docs, spans)
    change = next(d for d in result.decisions if d['decision'] == 'EXPAND')
    assert (change['previous_start'], change['previous_end']) == spans['repeated']
    assert_preserved(base, result)


def test_canonical_duplicate_is_not_added_back_as_new_document():
    facts, docs, spans = fixture()
    docs[1] = replace(docs[1], memory=replace(docs[0].memory, id='doc2'))
    base = prepare(facts, docs, spans)
    result, audit = extend(base, docs, spans)
    assert not audit['added_document_ids']
    assert len(result.items) == len(base.items)
    assert_preserved(base, result)


def test_invalid_budget_fails_instead_of_reinterpreting_the_baseline():
    facts, docs, spans = fixture()
    base = prepare(facts, docs, spans)
    with pytest.raises(ValueError, match='baseline'):
        extend(base, docs, spans, budget=base.estimated_tokens - 1)


def test_new_controls_are_opt_in_and_fingerprinted():
    assert not PipelineConfig().spare_document_context
    PipelineConfig(controlled_document_token_budget=5000)  # legacy config stays valid
    with pytest.raises(ValueError, match='controlled supplement'):
        PipelineConfig(spare_document_context=True)
    c = PipelineConfig(document_chunk_search_name='controlled_supplement',
        document_selection_name='query_excerpt', scoring_policy_name='balanced', spare_document_context=True)
    assert c.to_dict()['spare_document_context']
    assert c.to_dict()['spare_document_token_budget'] == 2400
    assert c.to_dict()['spare_document_char_budget'] == 4800


def test_pipeline_records_spare_audit_only_when_enabled():
    from dive_memory.oracle.pipeline import run_case
    from dive_memory.oracle.reader import DeterministicReader
    from test_oracle_pipeline import _case
    config = PipelineConfig(document_chunk_search_name='controlled_supplement',
        document_selection_name='query_excerpt', scoring_policy_name='balanced', token_budget=6000)
    before = run_case(_case(), reader=DeterministicReader(), config=config).record
    after = run_case(_case(), reader=DeterministicReader(), config=replace(config, spare_document_context=True)).record
    assert 'spare_document_expansion' not in before['packing']
    assert after['packing']['spare_document_expansion']['baseline_visible_preserved']
    assert after['reader_context_text'] == before['reader_context_text']
