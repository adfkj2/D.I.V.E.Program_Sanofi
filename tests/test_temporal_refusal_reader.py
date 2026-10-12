from dataclasses import replace
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import oracle_temporal_refusal_experiment as x
from dive_memory.oracle.temporal_refusal_reader import TemporalRefusalReader, temporal_question
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.premise_review_reader import NecessaryPremiseReader
from test_refusal_gated_reader import Reader, outcome
from test_cited_calculation import request as build_request
from test_oracle_user_document_experiment import metered, payload, request


@pytest.mark.parametrize('question',[
    'When did I move?', 'What time is my alarm?', 'How long did the trip take?',
    'How many weeks passed between signing and delivery?', 'How old will I be next year?',
    'Which seminar did I attend first?', 'Who arrived before Chris?',
    'What music did I hear last Thursday?', '我什么时候搬家？', '我等了多少天？'])
def test_temporal_query_form_is_included(question):
    assert temporal_question(question)


@pytest.mark.parametrize('question',[
    'How many books did I read last month?', 'How many times did I visit last week?',
    'How many museums did I see in July?', 'At which university did I present a poster?',
    'What is the total cost of two items?', 'Which address is current?', 'Where is my phone?'])
def test_non_temporal_result_is_not_routed(question):
    assert not temporal_question(question)


@pytest.mark.parametrize('status',list(ReaderStatus))
def test_only_valid_refusal_can_get_a_review(status):
    first=Reader(outcome(status,'known' if status==ReaderStatus.ANSWER else None))
    reviewer=Reader(outcome(ReaderStatus.ANSWER,'derived'))
    req=build_request(['I left at 08:20.'],question='What time did I leave?')
    result=TemporalRefusalReader(first,reviewer).generate(req)
    assert len(reviewer.requests)==(status==ReaderStatus.ABSTAIN)
    assert result.status==(ReaderStatus.ANSWER if status==ReaderStatus.ABSTAIN else status)
    assert result.calls==first.outcome.calls+(reviewer.outcome.calls if status==ReaderStatus.ABSTAIN else 0)


def test_review_failure_preserved_and_no_prior_answer_in_prompt(tmp_path):
    meter,normal,thinking=metered(tmp_path,[payload('ABSTAIN')],[payload('ABSTAIN'),'broken','broken'])
    req=replace(request(),question='What time did I buy the tickets?')
    reader=x.make_reader(meter)
    result=reader.generate(req)
    assert result.status==ReaderStatus.INVALID
    assert reader.reviewer.rules==NecessaryPremiseReader.rules
    messages=thinking.requests[1]['messages']
    assert set(json.loads(messages[1]['content']))=={'question','question_date','as_of','context'}
    assert len(json.loads(result.detail)['stages'])==2


def test_question_labels_cannot_route_the_reader():
    with pytest.raises(TypeError):temporal_question({'question_type':'temporal-reasoning'})
