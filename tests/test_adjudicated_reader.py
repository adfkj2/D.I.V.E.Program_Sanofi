import json
import pytest

from dive_memory.oracle.adjudicated_reader import AdjudicatedReader, EvidenceReferee, REFEREE_VERSION
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.reader import ReaderSpec, reader_policy
from test_refusal_gated_reader import Reader, outcome
from test_cited_calculation import request


@pytest.mark.parametrize("primary_status", [ReaderStatus.ANSWER, ReaderStatus.ABSTAIN])
def test_opt_in_format_recovery_preserves_valid_primary_and_all_usage(primary_status):
    primary = Reader(outcome(primary_status, "Kyoto" if primary_status == ReaderStatus.ANSWER else None))
    secondary = Reader(outcome(ReaderStatus.INVALID))
    referee = Reader(outcome(ReaderStatus.ABSTAIN))
    baseline = AdjudicatedReader(primary, secondary, referee)
    candidate = AdjudicatedReader(primary, secondary, referee, recover_invalid_secondary=True)
    assert baseline.generate(object()).status == ReaderStatus.INVALID
    result = candidate.generate(object())
    assert result.status == primary_status and result.calls == 2
    assert result.usage.input_tokens == primary.outcome.usage.input_tokens + secondary.outcome.usage.input_tokens
    assert json.loads(result.detail)["route"] == "primary_after_secondary_invalid"
    assert not referee.requests
    assert candidate.spec.fingerprint() != baseline.spec.fingerprint()


@pytest.mark.parametrize("primary_status,secondary_status", [
    (ReaderStatus.INVALID, ReaderStatus.INVALID),
    (ReaderStatus.ANSWER, ReaderStatus.ERROR),
    (ReaderStatus.ANSWER, ReaderStatus.TIMEOUT),
])
def test_format_recovery_never_hides_transport_or_double_format_failures(primary_status, secondary_status):
    result = AdjudicatedReader(Reader(outcome(primary_status)), Reader(outcome(secondary_status)),
        Reader(outcome(ReaderStatus.ABSTAIN)), recover_invalid_secondary=True).generate(object())
    assert result.status == secondary_status and result.is_failure


def test_format_recovery_does_not_override_a_genuine_reviewed_refusal():
    referee = Reader(outcome(ReaderStatus.ABSTAIN))
    result = AdjudicatedReader(Reader(outcome(ReaderStatus.ANSWER, "guess")),
        Reader(outcome(ReaderStatus.ABSTAIN)), referee,
        recover_invalid_secondary=True).generate(object())
    assert result.status == ReaderStatus.ABSTAIN and len(referee.requests) == 1
    assert json.loads(result.detail)["route"] == "independent_evidence_review"


def test_false_refusal_can_be_recovered_only_after_independent_review():
    primary = Reader(outcome(ReaderStatus.ABSTAIN))
    secondary = Reader(outcome(ReaderStatus.ANSWER, "Kyoto"))
    referee = Reader(outcome(ReaderStatus.ANSWER, "Kyoto"))
    question = object()
    result = AdjudicatedReader(primary, secondary, referee).generate(question)
    assert result.hypothesis == "Kyoto" and result.calls == 3
    assert primary.requests == secondary.requests == referee.requests == [question]
    assert json.loads(result.detail)["route"] == "independent_evidence_review"


def test_referee_can_reject_an_unsupported_secondary_answer():
    readers = [Reader(outcome(ReaderStatus.ABSTAIN)), Reader(outcome(ReaderStatus.ANSWER, "guess")),
               Reader(outcome(ReaderStatus.ABSTAIN))]
    result = AdjudicatedReader(*readers).generate(object())
    assert result.status == ReaderStatus.ABSTAIN and result.hypothesis is None


def test_agreement_does_not_add_a_review_call():
    primary = Reader(outcome(ReaderStatus.ANSWER, "first"))
    secondary = Reader(outcome(ReaderStatus.ANSWER, "second"))
    referee = Reader(outcome(ReaderStatus.ABSTAIN))
    result = AdjudicatedReader(primary, secondary, referee).generate(object())
    assert result.hypothesis == "second" and result.calls == 2 and not referee.requests


def test_transport_failure_stops_but_invalid_format_can_be_recovered():
    secondary = Reader(outcome(ReaderStatus.ANSWER, "second"))
    referee = Reader(outcome(ReaderStatus.ABSTAIN))
    result = AdjudicatedReader(Reader(outcome(ReaderStatus.ERROR)), secondary, referee).generate(object())
    assert result.is_failure and not secondary.requests and not referee.requests
    result = AdjudicatedReader(Reader(outcome(ReaderStatus.INVALID)), secondary, referee).generate(object())
    assert result.hypothesis == "second" and result.calls == 2 and not referee.requests


def test_referee_still_checks_visible_citations_and_uses_only_reader_request():
    quote = "I live in Kyoto."
    client = ScriptedChatClient(responses=[json.dumps({"status":"ANSWER", "answer":"Kyoto",
        "citations":[{"index":0,"quote":quote}], "missing":[]})])
    referee = EvidenceReferee(client, ReaderSpec("test", "test", prompt_version=REFEREE_VERSION),
                             policy=reader_policy("indexed_evidence"))
    result = referee.generate(request([quote], question="Where do I live?"))
    assert result.hypothesis == "Kyoto"
    assert "Two readers disagreed" in client.prompts[0]
    for forbidden in ("question_id", "question_type", "should_abstain", "gold_answer"):
        assert forbidden not in client.prompts[0]
