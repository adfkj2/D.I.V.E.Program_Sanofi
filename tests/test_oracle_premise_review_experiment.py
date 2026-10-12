from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from oracle_premise_review_experiment import restore_visible_request
from dive_memory.oracle.reader import build_evidence_packet
from test_cited_calculation import request


def saved():
    req = request(['I own two red folders.', 'I own three blue folders.'],
                  question='How many folders do I own?')
    views, _, _ = build_evidence_packet(req)
    return req, {'namespace': 'test', 'as_of': req.as_of,
                 'reader_context_text': req.context_text,
                 'packing': {'omitted': 9},
                 'evidence': {'views': [v.to_dict() for v in views]}}


def test_reconstruction_restores_only_visible_text_and_exact_provenance():
    req, trace = saved()
    trace['hidden_raw_history'] = 'The secret missing folder is green.'
    rebuilt = restore_visible_request(req.question, trace)
    assert rebuilt.context_text == req.context_text
    assert [i.memory.content for i in rebuilt.items] == [i.memory.content for i in req.items]
    assert [i.source_refs for i in rebuilt.items] == [i.source_refs for i in req.items]
    assert 'secret' not in rebuilt.context_text and rebuilt.evidence_omitted == 9


@pytest.mark.parametrize('field,value', [('role', 'assistant'), ('source_refs', ['different']),
                                       ('index', 3), ('valid_from', '2025-01-01')])
def test_metadata_cannot_silently_change_visible_request(field, value):
    req, trace = saved()
    trace = deepcopy(trace)
    trace['evidence']['views'][0][field] = value
    with pytest.raises(ValueError):
        restore_visible_request(req.question, trace)
