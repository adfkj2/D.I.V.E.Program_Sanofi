import json

import pytest

from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.reader import ReaderOutcome, ReaderSpec, TokenUsage, reader_policy
from dive_memory.oracle.refusal_gated_reader import RefusalGatedReader


class Reader:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.policy = reader_policy("indexed_evidence")
        self.spec = ReaderSpec("test", "test")

    def generate(self, request):
        self.requests.append(request)
        return self.outcome


def outcome(status, text=None, *, tokens=10):
    return ReaderOutcome(status, text, "test", calls=1,
                         usage=TokenUsage(tokens, tokens, "provider"))


@pytest.mark.parametrize("status", [ReaderStatus.ABSTAIN, ReaderStatus.INVALID,
                                  ReaderStatus.ERROR, ReaderStatus.TIMEOUT])
def test_primary_nonanswer_is_preserved_without_a_secondary_call(status):
    primary = Reader(outcome(status))
    secondary = Reader(outcome(ReaderStatus.ANSWER, "unsupported override"))
    result = RefusalGatedReader(primary, secondary).generate(object())
    assert result.status == status and result.calls == 1
    assert not secondary.requests
    assert json.loads(result.detail)["route"] == "primary_stop"


def test_secondary_uses_identical_evidence_without_primary_answer_injection():
    primary = Reader(outcome(ReaderStatus.ANSWER, "first", tokens=20))
    secondary = Reader(outcome(ReaderStatus.ANSWER, "second", tokens=30))
    request = object()
    result = RefusalGatedReader(primary, secondary).generate(request)
    assert primary.requests == secondary.requests == [request]
    assert result.hypothesis == "second" and result.calls == 2
    assert result.usage.input_tokens == result.usage.output_tokens == 50
    assert json.loads(result.detail)["primary"]["hypothesis"] == "first"


@pytest.mark.parametrize("status", [ReaderStatus.ABSTAIN, ReaderStatus.INVALID, ReaderStatus.ERROR])
def test_secondary_refusal_or_failure_cannot_be_hidden_by_primary_answer(status):
    primary = Reader(outcome(ReaderStatus.ANSWER, "first"))
    secondary = Reader(outcome(status))
    result = RefusalGatedReader(primary, secondary).generate(object())
    assert result.status == status and result.calls == 2
    assert result.is_failure == (status != ReaderStatus.ABSTAIN)


def test_unknown_usage_is_not_converted_to_zero():
    primary = Reader(outcome(ReaderStatus.ANSWER, "first", tokens=None))
    secondary = Reader(outcome(ReaderStatus.ANSWER, "second"))
    result = RefusalGatedReader(primary, secondary).generate(object())
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    assert result.usage.source == "mixed_or_unknown"
