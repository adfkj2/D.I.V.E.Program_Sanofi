"""Restore source clocks and roles without changing old experiment drivers."""
from dive_memory.models import Memory, MemoryStatus, EvidenceState, RetrievalItem
from dive_memory.oracle.reader import OracleReaderRequest, reader_policy, render_reader_context
from dive_memory.token_budget import ApproximateTokenCounter
from audit_adjudicated_result import visible_passages


def restore_visible_request(question, trace):
    passages = visible_passages(trace)
    items = []
    statuses = {'CURRENT': MemoryStatus.ACTIVE, 'SUPERSEDED': MemoryStatus.SUPERSEDED,
                'ARCHIVED': MemoryStatus.ARCHIVED, 'REMOVED': MemoryStatus.DELETED}
    for index, view in enumerate(trace['evidence']['views']):
        if index != view['index']:
            raise ValueError('Noncontiguous evidence indices')
        content, refs = passages[view['memory_id']]
        if refs != set(view['source_refs']):
            raise ValueError('Source mismatch')
        structured = {'role': view['role']}
        for external, internal in [('statement_sources', '_reader_statement_sources'),
                                   ('source_positions', '_reader_sources')]:
            if external in view:
                structured[internal] = view[external]
        memory = Memory(view['memory_id'], trace['namespace'], 'visible_replay',
            EvidenceState.OBSERVATION, content, structured,
            status=statuses[view['lifecycle']], valid_from=view['valid_from'],
            valid_to=view['valid_to'], observed_at=view.get('observed_at'),
            contradicts_id=view['conflict_group'])
        items.append(RetrievalItem(memory, view['retrieval_score'],
                                  channels=view['channels'], source_refs=view['source_refs']))
    request = OracleReaderRequest(question, trace['as_of'], trace['as_of'], tuple(items),
        trace['reader_context_text'], token_budget=6000, policy=reader_policy('indexed_evidence'),
        evidence_omitted=trace['packing']['omitted'])
    if render_reader_context(request) != trace['reader_context_text']:
        raise ValueError('Restored visible request differs from audited context')
    if ApproximateTokenCounter().count(request.context_text) > request.token_budget:
        raise ValueError('Restored context exceeds the original budget')
    return request
