import json
import pytest

from dive_memory.oracle.premise_review_reader import (
    NecessaryPremiseReader, RefusalReviewReader, REVIEW_VERSION,
)
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.reader import ReaderSpec, reader_policy
from test_refusal_gated_reader import Reader, outcome
from test_cited_calculation import request


@pytest.mark.parametrize('status', [ReaderStatus.ANSWER, ReaderStatus.INVALID,
                                  ReaderStatus.ERROR, ReaderStatus.TIMEOUT])
def test_review_cannot_replace_answers_or_hide_prior_failure(status):
    first = Reader(outcome(status, 'known' if status == ReaderStatus.ANSWER else None))
    reviewer = Reader(outcome(ReaderStatus.ANSWER, 'different'))
    result = RefusalReviewReader(first, reviewer).generate(object())
    assert result.status == status and result.hypothesis == first.outcome.hypothesis
    assert not reviewer.requests and result.calls == first.outcome.calls


@pytest.mark.parametrize('status', list(ReaderStatus))
def test_refusal_gets_only_one_review_preserving_failures_and_usage(status):
    first = Reader(outcome(ReaderStatus.ABSTAIN))
    reviewer = Reader(outcome(status, 'six' if status == ReaderStatus.ANSWER else None))
    req = object()
    result = RefusalReviewReader(first, reviewer).generate(req)
    assert result.status == status and reviewer.requests == [req]
    assert result.calls == first.outcome.calls + reviewer.outcome.calls
    assert result.usage.input_tokens == first.outcome.usage.input_tokens + reviewer.outcome.usage.input_tokens
    stages = json.loads(result.detail)['stages']
    assert stages == [first.outcome.to_dict(), reviewer.outcome.to_dict()]


def test_premise_review_uses_only_budget_visible_request_and_checks_citations():
    quote = 'My usual alarm is at 08:10; on Fridays I set it 20 minutes earlier.'
    answer = {'status': 'ANSWER', 'answer': '07:50',
              'citations': [{'index': 0, 'quote': quote}], 'missing': []}
    client = ScriptedChatClient(responses=[json.dumps(answer)])
    reviewer = NecessaryPremiseReader(client, ReaderSpec('test', 'test', prompt_version=REVIEW_VERSION),
                                      policy=reader_policy('indexed_evidence'))
    req = request([quote], question='When is my alarm on Fridays?')
    primary = Reader(outcome(ReaderStatus.ABSTAIN))
    result = RefusalReviewReader(primary, reviewer).generate(req)
    assert result.hypothesis == '07:50' and len(result.citations) == 1
    for forbidden in ('question_id', 'question_type', 'should_abstain', 'gold_answer',
                      'missing_requirements', 'previous_attempt'):
        assert forbidden not in client.prompts[0]
    answer['citations'][0]['quote'] = 'My usual alarm is at 07:50.'
    bad = ScriptedChatClient(responses=[json.dumps(answer), json.dumps(answer)])
    invalid = NecessaryPremiseReader(bad, reviewer.spec, policy=reviewer.policy).generate(req)
    assert invalid.status == ReaderStatus.INVALID
