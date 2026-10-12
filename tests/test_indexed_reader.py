import json
from dataclasses import replace
import pytest
from dive_memory.models import Memory, RetrievalItem, EvidenceState
from dive_memory.oracle.indexed_reader import IndexedEvidenceReader, VERSION, parse_indexed
from dive_memory.oracle.reader import ReaderSpec, OracleReaderRequest, render_reader_context, reader_policy
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.source_diversity import diversify_sources


def item(id="m1", refs=("event1",)):
    return RetrievalItem(Memory(id, "u", "episode", EvidenceState.OBSERVATION,
        "I bought four tickets for 80 dollars.", {"role": "user"}), 1, source_refs=list(refs))


def request():
    r = OracleReaderRequest("What did the tickets cost?", None, None, (item(),), "",
                             policy=reader_policy("indexed_evidence"))
    return replace(r, context_text=render_reader_context(r))


def payload(index=0, quote="four tickets for 80 dollars"):
    return json.dumps({"status": "ANSWER", "answer": "80 dollars", "citations": [
        {"index": index, "quote": quote}], "missing": []})


def test_program_resolves_ids_without_weakening_quote_contract():
    d = parse_indexed(payload(), [item()])
    assert d.status.value == "ANSWER"
    assert d.citations[0].memory_id == "m1" and d.citations[0].source_refs == ("event1",)


def test_only_empty_success_diagnostics_can_be_omitted():
    obj = json.loads(payload()); del obj["missing"]
    assert parse_indexed(json.dumps(obj), [item()]).status.value == "ANSWER"
    obj["citations"][0]["quote"] = "invented"
    assert parse_indexed(json.dumps(obj), [item()]).reason_code == "UNSUPPORTED_READER_OUTPUT"
    obj = {"status": "ABSTAIN", "answer": None, "citations": []}
    assert parse_indexed(json.dumps(obj), [item()]).reason_code == "UNSUPPORTED_READER_OUTPUT"


@pytest.mark.parametrize("index,quote", [(True, "four"), (-1, "four"), (1, "four"), (0, "invented quote"), (0, "")])
def test_bad_citation_rejected(index, quote):
    assert parse_indexed(payload(index, quote), [item()]).reason_code == "UNSUPPORTED_READER_OUTPUT"


def test_invalid_quote_gets_one_repair_not_silent_acceptance():
    c = ScriptedChatClient(responses=[payload(quote="invented quote"), payload()])
    r = IndexedEvidenceReader(c, ReaderSpec("test", "test", prompt_version=VERSION))
    outcome = r.generate(request())
    assert outcome.status.value == "ANSWER" and outcome.calls == 2
    assert len(outcome.attempt_errors) == 1
    assert "format_repair" in c.prompts[1]


def test_genuine_refusal_is_not_reasked_and_invalid_refusal_stays_invalid():
    raw = json.dumps({"status": "ABSTAIN", "answer": None, "citations": [], "missing": ["fare"]})
    c = ScriptedChatClient(responses=[raw])
    r = IndexedEvidenceReader(c, ReaderSpec("test", "test", prompt_version=VERSION))
    assert r.generate(request()).calls == 1
    obj = json.loads(raw); obj["answer"] = "80"
    assert parse_indexed(json.dumps(obj), [item()]).reason_code == "UNSUPPORTED_READER_OUTPUT"


def test_hidden_tail_is_not_available_to_indexed_reader():
    r = IndexedEvidenceReader(ScriptedChatClient(), ReaderSpec("test", "test", prompt_version=VERSION))
    with pytest.raises(ValueError, match="budget-visible"):
        r.generate(replace(request(), context_text="unrelated"))


def test_diversity_preserves_distinct_provenance_before_duplicates():
    a, b, c = item(), item("m2"), item("m3", ("event2",))
    chosen, trace = diversify_sources([a, b, c], 2)
    assert [x.memory.id for x in chosen] == ["m1", "m3"]
    assert trace["pool_size"] == 3
