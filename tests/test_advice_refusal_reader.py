import json
from dataclasses import replace
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import oracle_advice_refusal_experiment as x
from dive_memory.oracle.advice_refusal_reader import AdviceRefusalReader, advice_question
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.premise_review_reader import NecessaryPremiseReader
from test_refusal_gated_reader import Reader, outcome
from test_cited_calculation import request as build_request
from test_oracle_user_document_experiment import metered, payload, request


@pytest.mark.parametrize('question',[
    'Can you recommend a quiet restaurant?', 'Could you suggest a new exercise?',
    'Any tips for learning to paint?', 'I want to host a party. Any advice?',
    'Do you think I should join the club?', 'What should I cook?',
    'Which bike should I buy?', 'What do you think?', '你可以推荐一些活动吗？'])
def test_current_advice_request(question):
    assert advice_question(question)


@pytest.mark.parametrize('question',[
    'Can you remind me what you recommended?',
    'Do you remember which restaurant you suggested?',
    'What were the tips in our previous conversation?',
    'Can you suggest the name you previously recommended?',
    'How much did I spend?', 'What should I have paid last time?',
    '你还记得上次推荐的书吗？', 'What hotel did I book?'])
def test_recollection_or_fact_request_is_excluded(question):
    assert not advice_question(question)


@pytest.mark.parametrize('status',list(ReaderStatus))
def test_only_valid_refusal_reviewed(status):
    first=Reader(outcome(status,'known' if status==ReaderStatus.ANSWER else None))
    reviewer=Reader(outcome(ReaderStatus.ANSWER,'advice'))
    req=build_request(['I enjoy hiking.'],question='Can you suggest an activity?')
    result=AdviceRefusalReader(first,reviewer).generate(req)
    assert len(reviewer.requests)==(status==ReaderStatus.ABSTAIN)
    assert result.status==(ReaderStatus.ANSWER if status==ReaderStatus.ABSTAIN else status)
    assert result.calls==first.outcome.calls+(reviewer.outcome.calls if status==ReaderStatus.ABSTAIN else 0)


def test_non_advice_refusal_passes_without_review():
    first=Reader(outcome(ReaderStatus.ABSTAIN,None));reviewer=Reader(outcome(ReaderStatus.ANSWER,'x'))
    req=build_request(['I like green.'],question='What is my passport number?')
    assert AdviceRefusalReader(first,reviewer).generate(req).status==ReaderStatus.ABSTAIN
    assert not reviewer.requests


def test_failure_preserved_and_review_sees_only_request(tmp_path):
    meter,normal,thinking=metered(tmp_path,[payload('ABSTAIN')],[payload('ABSTAIN'),'broken','broken'])
    reader=x.make_reader(meter)
    result=reader.generate(replace(request(),question='Can you suggest a hobby?'))
    assert result.status==ReaderStatus.INVALID
    assert reader.reviewer.rules==NecessaryPremiseReader.rules
    messages=thinking.requests[1]['messages']
    assert set(json.loads(messages[1]['content']))=={'question','question_date','as_of','context'}
    assert len(json.loads(result.detail)['stages'])==2


def test_labels_are_not_question_text():
    with pytest.raises(TypeError):advice_question({'question_type':'preference'})
