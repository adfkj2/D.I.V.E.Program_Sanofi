from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from oracle_source_retention_experiment import same_input, offline_eligible, generate_record, make_reader
from test_oracle_premise_review_experiment import saved
from oracle_premise_review_experiment import restore_visible_request
from dive_memory.oracle.contracts import ReaderStatus, HypothesisOrigin
from dive_memory.oracle.reader import ReaderOutcome, TokenUsage


def test_reuse_needs_all_visible_input_fields():
    req, trace = saved()
    a = restore_visible_request(req.question, trace)
    assert same_input(a, a)
    for kwargs in ({'question': 'A different question?'}, {'context_text': a.context_text + 'more'},
                   {'as_of': '2026-01-01'}, {'evidence_omitted': 10}, {'token_budget': 8000}):
        assert not same_input(a, replace(a, **kwargs))


def test_source_coverage_gain_does_not_override_a_complete_case_loss():
    before = dict(packed=50, complete_positive_cases=40, by_type_packed={'temporal': 20}, visible_chars=5000)
    assert not offline_eligible(before, before)
    assert offline_eligible(before, {**before, 'packed': 51})
    assert not offline_eligible(before, {**before, 'packed': 51, 'complete_positive_cases': 39})
    assert not offline_eligible(before, {**before, 'packed': 51, 'by_type_packed': {'temporal': 18}})


def test_uniform_reuse_preserves_saved_refusal_without_calling_reader():
    req, parent = saved()
    reader = make_reader(None, None, None)
    outcome = ReaderOutcome(status=ReaderStatus.ABSTAIN, hypothesis=None,
        origin=HypothesisOrigin.READER_ABSTENTION, reason_code='MISSING_PREMISE',
        calls=2, latency_ms=150, usage=TokenUsage(200, 30, 'provider'),
        detail=json.dumps({'route': 'same_answerability', 'stages': []}))
    parent.update(reader={**outcome.to_dict(), 'spec': reader.spec.to_dict()},
        hypothesis=outcome.resolved_hypothesis, hypothesis_origin=outcome.origin.value,
        status='COMPLETED', failure=None, timing_ms={'read':150, 'total':170}, calls={'reader':2}, tokens={})
    candidate = deepcopy(parent)
    candidate['timing_ms'] = {'read': 0, 'total': 20}
    # A reused input must never call a reader (this object deliberately cannot).
    record = generate_record(SimpleNamespace(question=req.question), candidate, parent, object())
    assert record['hypothesis'] == parent['hypothesis']
    assert record['reader'] == parent['reader']
    assert record['calls']['reader'] == 2
    assert record['tokens']['reader_output'] == 30
    assert record['timing_ms']['total'] == 170
    assert record['replay']['exact_parent_input_reused']
