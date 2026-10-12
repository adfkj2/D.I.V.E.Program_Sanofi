import json
from copy import deepcopy
from dataclasses import replace

import pytest

from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.oracle.premise_ledger_reader import (
    INVALID, VERSION, PremiseLedgerReader, parse_ledger,
)
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.reader import (
    ChatCompletion, OracleReaderRequest, ReaderSpec, reader_policy, render_reader_context,
)


def items():
    texts = ("I bought four tickets for 80 dollars.", "The same tickets had a 12 dollar fee.")
    return tuple(RetrievalItem(Memory(f"m{i}", "u", "episode", EvidenceState.OBSERVATION,
        text, {"role": "user"}), 1, source_refs=[f"event{i}"]) for i, text in enumerate(texts))


def request():
    req = OracleReaderRequest("What was the total ticket cost?", None, None, items(), "",
                              policy=reader_policy("indexed_evidence"))
    return replace(req, context_text=render_reader_context(req))


def payload():
    return {"target": "User's ticket purchase total, including fee", "ledger": [
        {"premise": "Tickets cost 80 dollars", "identity": "user's same ticket purchase",
         "time": "not needed", "state": "SUPPORTED",
         "citations": [{"index": 0, "quote": "four tickets for 80 dollars"}]},
        {"premise": "The fee was 12 dollars", "identity": "same tickets",
         "time": "not needed", "state": "SUPPORTED",
         "citations": [{"index": 1, "quote": "The same tickets had a 12 dollar fee."}]}],
        "derivation": "80 + 12 = 92", "status": "ANSWER", "answer": "92 dollars", "missing": []}


def refusal():
    obj = payload()
    obj.update(status="ABSTAIN", answer=None, derivation="", missing=["refund amount"])
    obj["ledger"].append({"premise": "Refund amount", "identity": "same tickets",
        "time": "not needed", "state": "MISSING", "citations": []})
    return obj


def reader(client):
    return PremiseLedgerReader(client, ReaderSpec("test", "test", prompt_version=VERSION,
                                                 max_tokens=1600))


def parse(obj, evidence=None):
    return parse_ledger(json.dumps(obj), items() if evidence is None else evidence)[0]


def test_derived_answer_resolves_all_premises_and_keeps_audit():
    client = ScriptedChatClient(responses=[json.dumps(payload())],
                               input_tokens_per_call=101, output_tokens_per_call=53)
    outcome = reader(client).generate(request())
    assert outcome.status.value == "ANSWER"
    assert outcome.hypothesis == "92 dollars"  # need not occur in a quote
    assert [(c["memory_id"], c["source_refs"]) for c in outcome.citations] == [
        ("m0", ["event0"]), ("m1", ["event1"])]
    assert outcome.usage.input_tokens == 101 and outcome.usage.output_tokens == 53
    assert outcome.usage.source == "provider" and outcome.calls == 1
    detail = json.loads(outcome.detail)
    assert detail["attempts"][0]["output"] == payload()
    assert detail["validation_scope"] == "schema_consistency_and_quote_provenance_only"
    assert outcome.policy_version == VERSION


def test_exact_duplicate_citations_are_deduplicated_without_losing_ledger():
    obj = payload()
    obj["ledger"].append(deepcopy(obj["ledger"][0]))
    decision, audit = parse_ledger(json.dumps(obj), items())
    assert len(decision.citations) == 2 and len(audit["ledger"]) == 3


@pytest.mark.parametrize("index,quote", [
    (True, "four"), (-1, "four"), (2, "four"), (0, "invented"), (0, ""),
    (0, "four tickets for 80  dollars"), ("0", "four"),
])
def test_invalid_provenance_is_rejected(index, quote):
    obj = payload()
    obj["ledger"][0]["citations"] = [{"index": index, "quote": quote}]
    assert parse(obj).reason_code == INVALID


def test_missing_source_refs_and_hidden_tail_are_rejected():
    evidence = list(items())
    evidence[0] = replace(evidence[0], source_refs=[])
    assert parse(payload(), evidence).reason_code == INVALID
    evidence[0] = replace(items()[0], memory=replace(items()[0].memory, content="I bought"))
    assert parse(payload(), evidence).reason_code == INVALID


@pytest.mark.parametrize("mutation", [
    lambda o: o.update(status="ERROR"),
    lambda o: o.update(ledger=[]),
    lambda o: o.update(target=""),
    lambda o: o.update(extra="unexpected"),
    lambda o: o.update(answer=None),
    lambda o: o.update(missing=["something"]),
    lambda o: o["ledger"][0].update(state="MISSING"),
    lambda o: o["ledger"][0].update(citations=[]),
    lambda o: o["ledger"][0].update(identity=""),
    lambda o: o["ledger"][0].update(time=None),
    lambda o: o["ledger"][0].update(state="DERIVED"),
])
def test_malformed_or_inconsistent_ledger_cannot_become_an_answer(mutation):
    obj = payload()
    mutation(obj)
    assert parse(obj).reason_code == INVALID


def test_duplicate_json_keys_are_rejected():
    text = json.dumps(payload()).replace('"status": "ANSWER"', '"status": "ABSTAIN", "status": "ANSWER"')
    assert parse_ledger(text, items())[0].reason_code == INVALID


def test_supported_ledger_does_not_claim_semantic_verification():
    obj = payload()
    obj["ledger"][0]["identity"] = "an unsupported different event"
    # The quote is valid but the identity judgment is wrong. This needs model/
    # external evaluation: structural validation must not pretend to prove it.
    assert parse(obj).status.value == "ANSWER"


def test_refusal_keeps_evidence_audit_without_answer_citations_or_reasking():
    obj = refusal()
    client = ScriptedChatClient(responses=[json.dumps(obj)])
    result = reader(client).generate(request())
    assert result.status.value == "ABSTAIN" and result.calls == 1
    assert not result.citations
    assert json.loads(result.detail)["attempts"][0]["output"] == obj
    obj["ledger"][0]["citations"][0]["quote"] = "invented"
    assert parse(obj).reason_code == INVALID  # citations on refusals also checked


@pytest.mark.parametrize("mutation", [
    lambda o: o.update(answer="92 dollars"),
    lambda o: o.update(missing=[]),
    lambda o: o.update(missing=[""]),
    lambda o: o["ledger"].pop(),
])
def test_inconsistent_abstention_remains_invalid(mutation):
    obj = refusal()
    mutation(obj)
    assert parse(obj).reason_code == INVALID


def test_format_repair_is_bounded_and_includes_every_completed_attempt():
    client = ScriptedChatClient(responses=["not JSON", json.dumps(payload())],
                               input_tokens_per_call=100, output_tokens_per_call=25)
    result = reader(client).generate(request())
    assert result.status.value == "ANSWER" and result.calls == 2
    assert (result.usage.input_tokens, result.usage.output_tokens) == (200, 50)
    assert len(result.attempt_errors) == 1 and len(json.loads(result.detail)["attempts"]) == 2
    assert "format_repair" in client.prompts[1]
    invalid = reader(ScriptedChatClient(responses=["bad", "bad", json.dumps(payload())])).generate(request())
    assert invalid.status.value == "INVALID" and invalid.calls == 2
    assert len(invalid.attempt_errors) == 2


class QueueClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.parametrize("error,status", [(TimeoutError("secret"), "TIMEOUT"),
                                          (RuntimeError("secret"), "ERROR")])
def test_transport_failure_has_unknown_usage_and_does_not_leak_error_text(error, status):
    client = QueueClient(error)
    outcome = reader(client).generate(request())
    assert outcome.status.value == status and outcome.calls == 1
    assert outcome.usage.input_tokens is None and outcome.usage.output_tokens is None
    assert outcome.usage.source == "unknown" and outcome.response_sha256 is None
    assert "secret" not in json.dumps(outcome.to_dict())


def test_repair_failure_keeps_observed_usage_and_all_attempts():
    client = QueueClient(ChatCompletion("bad", "test", input_tokens=40, output_tokens=3), TimeoutError())
    result = reader(client).generate(request())
    assert result.status.value == "TIMEOUT" and result.calls == 2
    assert (result.usage.input_tokens, result.usage.output_tokens) == (40, 3)
    assert result.usage.source == "provider_observed_only"
    assert len(result.attempt_errors) == 2 and len(json.loads(result.detail)["attempts"]) == 2


def test_partially_known_usage_retains_provider_counts_and_estimates_missing_fields():
    client = QueueClient(ChatCompletion(json.dumps(payload()), "test", input_tokens=11))
    result = reader(client).generate(request())
    assert result.usage.input_tokens == 11 and result.usage.output_tokens > 0
    assert result.usage.source == "mixed"
    assert client.calls[0]["max_tokens"] == 1600


def test_unknown_usage_is_estimated_and_cache_request_is_deterministic():
    completion = ChatCompletion(json.dumps(payload()), "test")
    first, second = QueueClient(completion), QueueClient(completion)
    a, b = reader(first).generate(request()), reader(second).generate(request())
    assert a.usage.source == "estimate" and a.usage.input_tokens > 0
    assert a.prompt_sha256 == b.prompt_sha256 and a.response_sha256 == b.response_sha256
    assert first.calls == second.calls


def test_unrendered_context_and_wrong_version_fail_before_any_request():
    client = QueueClient()
    with pytest.raises(ValueError, match="budget-visible"):
        reader(client).generate(replace(request(), context_text="hidden archive text"))
    with pytest.raises(ValueError, match="prompt version"):
        PremiseLedgerReader(client, ReaderSpec("test", "test"))
    assert not client.calls
