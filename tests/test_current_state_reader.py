import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import oracle_current_state_experiment as x
from dive_memory.oracle.current_state_reader import (
    CurrentStateReviewReader, CurrentStateReader, current_state_question, multiple_source_dates)
from dive_memory.oracle.contracts import ReaderStatus
from test_cited_calculation import item, request
from test_refusal_gated_reader import Reader, outcome
from test_oracle_user_document_experiment import metered, payload


@pytest.mark.parametrize('q',[
    'What is my current record?', 'Where do I currently live?',
    'Where did Chris move after the recent relocation?', 'What was my personal best time?',
    'What time do I wake up on weekdays?', 'What drink do I usually order?'])
def test_state_and_habit_questions(q):
    assert current_state_question(q)


@pytest.mark.parametrize('q',[
    'What was my previous personal best time?', 'Where did I initially keep my bike?',
    'How long have I lived in my current home?', 'How many tools do I currently own?',
    'Which events occurred from earliest to latest?',
    'Can you suggest a lens for my current camera?', 'What did I pay before the current price?'])
def test_excludes_history_counts_durations_order_and_advice(q):
    assert not current_state_question(q)


def test_dates_must_be_distinct_valid_and_visible():
    assert not multiple_source_dates(request([item('A'),item('B',1)]))
    assert multiple_source_dates(request([item('A',anchor='2024-01-01'),item('B',1,anchor='2024-02-01')]))
    assert not multiple_source_dates(request([item('A',anchor=None,observed='2024-01-01'),
        item('B',1,anchor=None,observed='2024-02-01')]))
    assert not multiple_source_dates(request([item('A',anchor='invalid'),item('B',1,anchor='2024-02-01')]))


@pytest.mark.parametrize('status',list(ReaderStatus))
def test_only_answer_can_be_reviewed_and_failure_is_preserved(status):
    first=Reader(outcome(status,'old' if status==ReaderStatus.ANSWER else None))
    reviewer=Reader(outcome(ReaderStatus.INVALID,None))
    req=request([item('Old',anchor='2024-01-01'),item('New',1,anchor='2024-02-01')],question='Where do I currently live?')
    result=CurrentStateReviewReader(first,reviewer).generate(req)
    assert len(reviewer.requests)==(status==ReaderStatus.ANSWER)
    assert result.status==(ReaderStatus.INVALID if status==ReaderStatus.ANSWER else status)
    if reviewer.requests:assert reviewer.requests==[req]


def test_factory_preserves_input_whitelist_and_body_quote_validation(tmp_path):
    bad=json.loads(payload());bad['citations'][0]['quote']='valid=2024-01-01'
    bad=json.dumps(bad)
    meter,normal,thinking=metered(tmp_path,[payload()],[payload(),bad,bad])
    req=request([item('I bought four tickets for 80 dollars.',anchor='2024-01-01'),
        item('I enjoy the new tickets.',1,anchor='2024-02-01')],question='What is my current record?')
    result=x.make_reader(meter).generate(req)
    assert result.status==ReaderStatus.INVALID
    assert len(thinking.requests)==3
    messages=thinking.requests[1]['messages']
    assert messages[0]['content']==CurrentStateReader.rules
    assert set(json.loads(messages[1]['content']))=={'question','question_date','as_of','context'}


def test_question_metadata_is_rejected():
    with pytest.raises(TypeError):current_state_question({'question_type':'knowledge-update'})
