import json
from dataclasses import replace

import pytest

from dive_memory.application.readers import create_reader
from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.reading.deliberation import VERSION
from dive_memory.reading.deliberative import DeliberativeEvidenceReader
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ChatCompletion, ReaderRequest, ReaderSpec


def item(text, memory_id="m1", refs=("event1",)):
    return RetrievalItem(
        Memory(memory_id, "project", "semantic_fact", EvidenceState.FACT, text, {}),
        1.0,
        ["lexical"],
        list(refs),
    )


def plan(*, notes=(), queries=(), reads=()):
    return {"notes": list(notes), "queries": list(queries), "reads": list(reads)}


def answer(text="73", quote="budget is 73", index=0):
    return {
        "status": "ANSWER",
        "answer": text,
        "citations": [{"index": index, "quote": quote}],
        "missing": [],
    }


ABSTAIN = {
    "status": "ABSTAIN",
    "answer": None,
    "citations": [],
    "missing": ["budget amount"],
}


class Source:
    def __init__(self, items=()):
        self.items = items
        self.searches = []
        self.reads = []

    def search(self, query):
        self.searches.append(query)
        return self.items

    def read(self, memory_id):
        self.reads.append(memory_id)
        return next(i for i in self.items if i.memory.id == memory_id)


def setup(responses, source=None, **kwargs):
    client = ScriptedChatClient(
        [r if isinstance(r, str) else json.dumps(r) for r in responses],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    reader = DeliberativeEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERSION),
        source=source or Source(),
        **kwargs,
    )
    return reader, client


def request(items=(), budget=1500):
    return ReaderRequest(
        "What is the budget?", None, None, tuple(items), "", token_budget=budget
    )


def payload(client, index):
    return json.loads(client.prompts[index].rsplit("\n", 1)[-1])


def test_search_gathers_evidence_then_review_corrects_a_grounded_draft():
    source = Source([item("The budget is 73.")])
    reader, client = setup([plan(queries=["budget"]), answer("74"), answer()], source)
    result = reader.generate(request())
    assert result.status.value == "ANSWER" and result.hypothesis == "73"
    assert source.searches == ["budget"] and result.calls == 3
    assert result.usage.input_tokens == 30 and result.usage.output_tokens == 15
    assert payload(client, 2)["untrusted_draft"]["answer"] == "74"
    audit = json.loads(result.detail)
    assert [turn["phase"] for turn in audit["turns"]] == [
        "plan",
        "synthesize",
        "review",
    ]
    assert all(
        p["context_tokens"] <= p["token_budget"] for p in audit["workspace_packets"]
    )


def test_review_cannot_promote_a_draft_claim_into_source_evidence():
    reader, _ = setup(
        [plan(), answer("73"), answer("73", quote="A fictional budget is 73.")]
    )
    result = reader.generate(request([item("The budget is 73.")]))
    # The final invalid quote is corrected once; an exhausted scripted client is
    # an explicit transport failure, never a reused draft or a true abstention.
    assert result.is_failure and result.hypothesis is None
    assert result.calls == 4


def test_review_can_reject_supported_quotes_that_do_not_answer_the_question():
    reader, _ = setup([plan(), answer("73"), ABSTAIN])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.status.value == "ABSTAIN" and not result.is_failure
    assert result.origin.value == "READER_ABSTENTION" and result.calls == 3


def test_notes_remain_visible_after_search_and_are_not_new_generated_facts():
    source = Source([item("The deadline is Friday.", "m2", ("event2",))])
    reader, client = setup(
        [
            plan(notes=[{"index": 0, "quote": "budget is 73"}], queries=["deadline"]),
            answer(),
            answer(),
        ],
        source,
    )
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.status.value == "ANSWER"
    for index in (1, 2):
        data = payload(client, index)
        assert "budget is 73" in data["context"]
        assert "deadline is Friday" in data["context"]
        assert data["selected_notes"][0]["quote"] == "budget is 73"


def test_read_uses_an_admitted_id_and_only_the_requested_window():
    original = item("Header. " + "unrelated. " * 150 + "The budget is 73.")
    initial = replace(original, memory=replace(original.memory, content="Header."))
    start = original.memory.content.index("The budget")
    source = Source([original])
    reader, client = setup(
        [plan(reads=[{"memory_id": "m1", "start": start}]), answer(), answer()], source
    )
    result = reader.generate(request([initial]))
    assert result.status.value == "ANSWER" and source.reads == ["m1"]
    assert "unrelated" not in payload(client, 1)["context"]


def test_offset_outside_memory_is_invalid_not_an_invented_abstention():
    source = Source([item("Header.")])
    reader, _ = setup([plan(reads=[{"memory_id": "m1", "start": 9999}])], source)
    result = reader.generate(request(source.items))
    assert (
        result.status.value == "INVALID" and result.reason_code == "INVALID_READ_OFFSET"
    )
    assert source.reads == ["m1"] and result.calls == 1


@pytest.mark.parametrize("bad_stage", [0, 1, 2])
def test_one_format_repair_is_shared_while_reserving_remaining_phases(bad_stage):
    replies = [plan(), answer(), answer()]
    replies.insert(bad_stage, "not json")
    reader, _ = setup(replies)
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.status.value == "ANSWER" and result.calls == 4
    assert sum("validation_error" in t for t in json.loads(result.detail)["turns"]) == 1


def test_spending_the_repair_on_planning_cannot_skip_review():
    reader, _ = setup(["not json", plan(), answer(), "not json"])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.status.value == "INVALID" and result.calls == 4
    assert result.reason_code == "INVALID_REVIEW_OUTPUT" and result.hypothesis is None


def test_bad_plan_is_not_executed_and_cannot_consume_required_answer_calls():
    source = Source([item("The budget is 73.")])
    reader, _ = setup(
        [plan(queries=["budget"], reads=[{"memory_id": "unknown", "start": 0}]), "bad"],
        source,
    )
    result = reader.generate(request())
    assert result.status.value == "INVALID" and result.calls == 2
    assert not source.searches and not source.reads


@pytest.mark.parametrize("fail_stage", [0, 1, 2])
def test_transport_failure_has_no_hidden_retry_or_partial_usage_total(fail_stage):
    class Broken:
        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            index = self.calls
            self.calls += 1
            if index == fail_stage:
                raise TimeoutError("fixture")
            return ChatCompletion(
                json.dumps([plan(), answer(), answer()][index]),
                "fixture",
                input_tokens=10,
                output_tokens=5,
            )

    reader, _ = setup([])
    reader.client = Broken()
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.status.value == "TIMEOUT" and result.calls == fail_stage + 1
    assert result.usage.input_tokens is result.usage.output_tokens is None
    assert reader.client.calls == fail_stage + 1


@pytest.mark.parametrize("operation", ["search", "read"])
def test_source_failure_is_reported_once_and_never_retried_as_model_format(operation):
    class Broken(Source):
        def search(self, query):
            self.searches.append(query)
            raise OSError("fixture")

        def read(self, memory_id):
            self.reads.append(memory_id)
            raise OSError("fixture")

    source = Broken()
    action = (
        plan(queries=["budget"])
        if operation == "search"
        else plan(reads=[{"memory_id": "m1", "start": 0}])
    )
    reader, _ = setup([action], source)
    result = reader.generate(request([item("Header.")]))
    assert (
        result.status.value == "ERROR"
        and result.reason_code == "EVIDENCE_SOURCE_FAILURE"
    )
    assert result.calls == 1 and len(source.searches) + len(source.reads) == 1


def test_repeated_generate_does_not_reuse_another_questions_workspace():
    reader, client = setup([plan(), answer(), answer(), plan(), ABSTAIN, ABSTAIN])
    assert reader.generate(request([item("The budget is 73.")])).hypothesis == "73"
    result = reader.generate(request())
    assert result.status.value == "ABSTAIN"
    assert "budget is 73" not in payload(client, 3)["context"]
    assert payload(client, 5)["untrusted_draft"]["answer"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_calls": 2},
        {"max_calls": True},
        {"max_calls": 5},
        {"passage_chars": 255},
        {"passage_chars": False},
    ],
)
def test_configuration_cannot_remove_required_phases_or_unbound_the_context(kwargs):
    with pytest.raises(ValueError):
        setup([], **kwargs)


def test_factory_and_binding_keep_experimental_strategy_explicit():
    client = ScriptedChatClient(
        [json.dumps(plan()), json.dumps(ABSTAIN), json.dumps(ABSTAIN)]
    )
    reader = create_reader(client, model="fixture", strategy="deliberative")
    assert isinstance(reader, DeliberativeEvidenceReader)
    with pytest.raises(ValueError, match="bound"):
        reader.generate(request())
    bound = reader.bind_evidence(Source())
    assert bound is not reader and reader.source is None
    assert bound.generate(request()).status.value == "ABSTAIN"
