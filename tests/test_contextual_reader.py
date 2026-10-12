"""Offline wiring/contract checks, not tests of a model's semantic accuracy."""

from dataclasses import fields, replace
import json
from types import SimpleNamespace

import pytest

from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.oracle.adjudicated_reader import EvidenceReferee
from dive_memory.oracle.contextual_reader import (
    CONTEXTUAL_RULES, REFEREE_VERSION, VERSION,
    ContextualEvidenceReader, ContextualEvidenceReferee,
)
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.indexed_reader import IndexedEvidenceReader, RULES
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.reader import OracleReaderRequest, ReaderSpec, reader_policy, render_reader_context


READERS = [(ContextualEvidenceReader, VERSION), (ContextualEvidenceReferee, REFEREE_VERSION)]
BODY = "I received the green parcel yesterday."


def request():
    memory = Memory("parcel-memory", "synthetic", "episode", EvidenceState.OBSERVATION,
                    BODY, {"role": "user"}, valid_from="2025-05-12", observed_at="2025-05-12")
    item = RetrievalItem(memory, 1.0, source_refs=["parcel-statement"])
    result = OracleReaderRequest("When did I receive the green parcel?", "2025-05-14",
                                 "2025-05-14", (item,), "", token_budget=6000,
                                 policy=reader_policy("indexed_evidence"))
    return replace(result, context_text=render_reader_context(result))


def answer(quote=BODY, index=0):
    return json.dumps({"status": "ANSWER", "answer": "May 11, 2025",
                       "citations": [{"index": index, "quote": quote}], "missing": []})


def refusal():
    return json.dumps({"status": "ABSTAIN", "answer": None, "citations": [],
                       "missing": ["necessary event identity"]})


class RecordingClient(ScriptedChatClient):
    def __init__(self, responses):
        super().__init__(responses=responses, input_tokens_per_call=7, output_tokens_per_call=5)
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(kwargs)
        return super().complete(**kwargs)


def reader(kind, version, responses):
    client = RecordingClient(responses)
    instance = kind(client, ReaderSpec("scripted", "scripted-chat-v1", prompt_version=version))
    return instance, client


def test_rules_are_the_only_override_and_are_shared_without_changing_parents():
    assert ContextualEvidenceReader.generate is IndexedEvidenceReader.generate
    assert ContextualEvidenceReferee.generate is EvidenceReferee.generate
    assert ContextualEvidenceReader.rules == RULES + CONTEXTUAL_RULES
    assert ContextualEvidenceReferee.rules == EvidenceReferee.rules + CONTEXTUAL_RULES
    assert CONTEXTUAL_RULES not in RULES
    assert CONTEXTUAL_RULES not in EvidenceReferee.rules
    assert VERSION != REFEREE_VERSION
    assert VERSION != IndexedEvidenceReader.prompt_version
    assert REFEREE_VERSION != EvidenceReferee.prompt_version


@pytest.mark.parametrize("kind,version", READERS)
def test_own_version_is_required_and_changes_request_fingerprint(kind, version):
    with pytest.raises(ValueError, match="own prompt version"):
        kind(ScriptedChatClient(), ReaderSpec("scripted", "test", prompt_version="wrong-version"))
    instance, _ = reader(kind, version, [answer()])
    parent = EvidenceReferee if kind is ContextualEvidenceReferee else IndexedEvidenceReader
    baseline = parent(ScriptedChatClient(responses=[answer()]),
                      ReaderSpec("scripted", "scripted-chat-v1", prompt_version=parent.prompt_version))
    assert instance.spec.fingerprint() != baseline.spec.fingerprint()
    assert instance.generate(request()).prompt_sha256 != baseline.generate(request()).prompt_sha256


@pytest.mark.parametrize("kind,version", READERS)
def test_payload_is_whitelisted_and_referee_gets_no_prior_decisions(kind, version):
    instance, client = reader(kind, version, [answer()])
    original = request()
    enriched = SimpleNamespace(**{field.name: getattr(original, field.name)
                                  for field in fields(original)},
                               question_id="PRIVATE_CASE", question_type="PRIVATE_TYPE",
                               gold_answer="PRIVATE_GOLD", should_abstain=False,
                               prior_answer="PRIVATE_PRIOR", prior_refusal="PRIVATE_REFUSAL")
    result = instance.generate(enriched)
    assert result.status is ReaderStatus.ANSWER
    messages = client.requests[0]["messages"]
    assert messages[0] == {"role": "system", "content": instance.rules}
    payload = json.loads(messages[1]["content"])
    assert payload == {"question": original.question, "question_date": original.question_date,
                       "as_of": original.as_of, "context": original.context_text}
    assert "PRIVATE_" not in client.prompts[0]
    assert client.requests[0]["response_format"] == {"type": "json_object"}
    assert result.citations[0]["memory_id"] == "parcel-memory"
    assert result.citations[0]["source_refs"] == ["parcel-statement"]


@pytest.mark.parametrize("kind,version", READERS)
@pytest.mark.parametrize("quote,index", [("valid=2025-05-12", 0), ("2025-05-12", 0),
                                        ("a fabricated quote", 0), (BODY, True), (BODY, 1)])
def test_invalid_quotes_or_indices_remain_invalid_after_one_repair(kind, version, quote, index):
    original = request()
    if quote.startswith("valid=") or quote == "2025-05-12":
        assert quote in original.context_text and quote not in BODY
    instance, client = reader(kind, version, [answer(quote, index), answer(quote, index)])
    result = instance.generate(original)
    assert result.status is ReaderStatus.INVALID and result.calls == 2
    assert len(result.attempt_errors) == 2
    assert result.usage.input_tokens == 14 and result.usage.output_tokens == 10
    repair_payload = json.loads(client.requests[1]["messages"][1]["content"])
    assert set(repair_payload) == {"question", "question_date", "as_of", "context", "format_repair"}
    assert "Recheck schema and exact quotes" in repair_payload["format_repair"]


@pytest.mark.parametrize("kind,version", READERS)
def test_format_repair_can_only_succeed_with_a_body_quote(kind, version):
    instance, client = reader(kind, version, [answer("valid=2025-05-12"), answer()])
    result = instance.generate(request())
    assert result.status is ReaderStatus.ANSWER and result.calls == 2
    assert len(result.attempt_errors) == 1
    assert result.citations[0]["quote"] == BODY
    assert len(client.requests) == 2


@pytest.mark.parametrize("kind,version", READERS)
def test_genuine_refusal_is_not_reasked_or_silently_changed(kind, version):
    instance, client = reader(kind, version, [refusal(), answer()])
    result = instance.generate(request())
    assert result.status is ReaderStatus.ABSTAIN and result.hypothesis is None
    assert result.calls == 1 and not result.attempt_errors
    assert len(client.requests) == 1


@pytest.mark.parametrize("kind,version", READERS)
def test_context_mismatch_is_rejected_before_client_call(kind, version):
    instance, client = reader(kind, version, [answer()])
    with pytest.raises(ValueError, match="budget-visible"):
        instance.generate(replace(request(), context_text="hidden or modified evidence"))
    assert not client.requests
