"""Scripted stage routing preserves admitted evidence through tools and recovery.

These are orchestration contracts, not model semantic-accuracy measurements.
"""

import json
import re
from dataclasses import replace

import pytest
from test_deliberative_reader import Source, item, request

from dive_memory.application.readers import create_reader
from dive_memory.reading.reader import ChatCompletion


def plan(indices=(), queries=(), reads=()):
    return {
        "source_indices": list(indices),
        "queries": list(queries),
        "reads": list(reads),
    }


def answer(indices, text="73"):
    return {
        "status": "ANSWER",
        "answer": text,
        "source_indices": list(indices),
        "missing": [],
    }


def abstain():
    return {
        "status": "ABSTAIN",
        "answer": None,
        "source_indices": [],
        "missing": ["The project budget was not recorded."],
    }


def payload(call):
    return json.loads(call["messages"][-1]["content"])


def index_for(context, memory_id):
    match = re.search(r"(?m)^\[(\d+)\] id=" + re.escape(memory_id) + r";", context)
    assert match is not None, f"Expected admitted source {memory_id}"
    return int(match.group(1))


class Client:
    provider = "fixture"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = self.replies.pop(0)
        if callable(value):
            value = value(payload(kwargs))
        if isinstance(value, ChatCompletion):
            return value
        return ChatCompletion(
            json.dumps(value), "fixture", input_tokens=10, output_tokens=5
        )


def setup(normal_values, thinking_values, source):
    normal, thinking = Client(normal_values), Client(thinking_values)
    reader = create_reader(
        normal, thinking, model="fixture", strategy="deliberative-staged"
    ).bind_evidence(source)
    return reader, normal, thinking


def test_missing_premise_is_searched_before_synthesis_and_is_bound_to_returned_source():
    source = Source([item("The approved budget is 73.", "approval", ("meeting",))])
    reader, normal, thinking = setup(
        [plan(queries=["approved budget"])], [answer([0]), answer([0])], source
    )
    result = reader.generate(request())
    assert result.hypothesis == "73" and result.calls == 3
    assert source.searches == ["approved budget"] and not source.reads
    assert payload(normal.calls[0])["context"] == ""
    assert "approved budget is 73" in payload(thinking.calls[0])["context"]
    assert result.citations[0]["memory_id"] == "approval"
    assert result.citations[0]["source_refs"] == ["meeting"]
    assert result.citations[0]["quote"] == "The approved budget is 73."


def test_requested_later_window_does_not_expose_unread_middle_of_document():
    original = item(
        "Project header. "
        + "Unrequested private appendix. " * 100
        + "The budget is 73."
    )
    initial = replace(
        original, memory=replace(original.memory, content="Project header.")
    )
    start = original.memory.content.index("The budget is 73.")
    source = Source([original])
    reader, _, thinking = setup(
        [plan(reads=[{"memory_id": "m1", "start": start}])],
        [answer([0]), answer([0])],
        source,
    )
    result = reader.generate(request([initial]))
    assert result.hypothesis == "73" and source.reads == ["m1"]
    assert not source.searches and result.calls == 3
    for call in thinking.calls:
        context = payload(call)["context"]
        assert "The budget is 73." in context
        assert "Unrequested private appendix" not in context
    assert result.citations[0]["quote"] == "The budget is 73."


def test_synthesis_recovery_uses_current_labels_and_review_keeps_old_and_new_sources():
    old = item("The former budget was 70.", "old-budget", ("old-event",))
    distractor = item("The project header is blue.", "header", ("header-event",))
    new = item("The budget was raised to 73.", "new-budget", ("new-event",))
    source = Source([new])
    empty = ChatCompletion("", "fixture", input_tokens=10, output_tokens=8192)

    def current_answer(data):
        old_index = index_for(data["context"], "old-budget")
        new_index = index_for(data["context"], "new-budget")
        return answer([old_index, new_index], "The budget increased from 70 to 73.")

    reader, normal, thinking = setup(
        [plan([1], queries=["budget raised"]), current_answer],
        [empty, current_answer],
        source,
    )
    result = reader.generate(request([distractor, old]))
    assert result.status.value == "ANSWER" and result.calls == 4
    assert source.searches == ["budget raised"]
    before = payload(normal.calls[0])
    recovery = payload(normal.calls[1])
    assert index_for(before["context"], "old-budget") == 1
    assert index_for(recovery["context"], "old-budget") == 0
    assert recovery["context"] == payload(thinking.calls[0])["context"]
    assert recovery["selected_notes"] == [
        {"index": 0, "quote": "The former budget was 70."}
    ]
    assert "empty" in recovery["validation_error"]
    assert {citation["memory_id"] for citation in result.citations} == {
        "old-budget",
        "new-budget",
    }
    assert {tuple(citation["source_refs"]) for citation in result.citations} == {
        ("old-event",),
        ("new-event",),
    }
    audit = json.loads(result.detail)
    assert [turn["phase"] for turn in audit["turns"]] == [
        "plan",
        "synthesize",
        "synthesize",
        "review",
    ]
    assert all(not packet["pin_errors"] for packet in audit["workspace_packets"])
    assert all(
        packet["context_tokens"] <= 1500 for packet in audit["workspace_packets"]
    )


@pytest.mark.parametrize(
    "bad_plan",
    [
        plan([4], queries=["budget"]),
        plan(queries=["budget"], reads=[{"memory_id": "unseen", "start": 0}]),
    ],
)
def test_invalid_source_or_unadmitted_read_blocks_all_tools_before_recovery(bad_plan):
    source = Source([item("The budget is 73.")])
    reader, normal, thinking = setup([bad_plan, bad_plan], [], source)
    result = reader.generate(request([item("Project header.")]))
    assert result.status.value == "INVALID" and result.calls == 2
    assert result.reason_code == "INVALID_PLAN_OUTPUT" and result.hypothesis is None
    assert not source.searches and not source.reads and not thinking.calls
    assert payload(normal.calls[1])["validation_error"] is not None


def test_repaired_plan_executes_only_its_valid_query_once():
    source = Source([item("The budget is 73.", "approval", ("meeting",))])
    reader, normal, thinking = setup(
        [plan([5], queries=["unapproved query"]), plan(queries=["approved budget"])],
        [answer([0]), answer([0])],
        source,
    )
    result = reader.generate(request())
    assert result.hypothesis == "73" and result.calls == 4
    assert source.searches == ["approved budget"]
    assert len(normal.calls) == 2 and len(thinking.calls) == 2


def test_a_draft_claim_does_not_become_an_additional_citable_source():
    source = Source()
    invented = "The budget is secretly 999."
    reader, normal, thinking = setup(
        [plan([0]), answer([1], invented)],
        [answer([0], invented), answer([1], invented)],
        source,
    )
    result = reader.generate(request([item("The project header is blue.")]))
    assert result.status.value == "INVALID" and result.calls == 4
    assert result.reason_code == "INVALID_REVIEW_OUTPUT" and result.hypothesis is None
    for call in (thinking.calls[1], normal.calls[1]):
        data = payload(call)
        assert data["untrusted_draft"]["answer"] == invented
        assert invented not in data["context"]
        assert "[1]" not in data["context"]
    assert not source.searches and not source.reads


def test_empty_search_can_end_in_explicit_abstention_without_invented_sources():
    source = Source()
    reader, _, thinking = setup(
        [plan(queries=["project budget"])], [abstain(), abstain()], source
    )
    result = reader.generate(request())
    assert result.status.value == "ABSTAIN" and not result.is_failure
    assert source.searches == ["project budget"] and result.calls == 3
    assert result.citations == ()
    assert all(payload(call)["context"] == "" for call in thinking.calls)


@pytest.mark.parametrize("operation", ["search", "read"])
def test_source_fault_does_not_spend_model_recovery_or_start_later_phases(operation):
    class Broken(Source):
        def search(self, query):
            self.searches.append(query)
            raise OSError("synthetic unavailable store")

        def read(self, memory_id):
            self.reads.append(memory_id)
            raise PermissionError("synthetic namespace denial")

    source = Broken()
    chosen_plan = (
        plan(queries=["budget"])
        if operation == "search"
        else plan(reads=[{"memory_id": "m1", "start": 0}])
    )
    reader, normal, thinking = setup([chosen_plan], [], source)
    result = reader.generate(request([item("Project header.")]))
    assert result.status.value == "ERROR" and result.calls == 1
    assert result.reason_code == "EVIDENCE_SOURCE_FAILURE"
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 5
    assert len(normal.calls) == 1 and not thinking.calls
    assert len(source.searches) + len(source.reads) == 1


def test_read_returning_another_id_is_not_admitted_as_evidence():
    class WrongSource(Source):
        def read(self, memory_id):
            self.reads.append(memory_id)
            return item("The secret budget is 91.", "another-project", ("secret",))

    source = WrongSource()
    reader, _, thinking = setup(
        [plan(reads=[{"memory_id": "m1", "start": 0}])], [], source
    )
    result = reader.generate(request([item("Project header.")]))
    assert result.status.value == "ERROR" and result.calls == 1
    assert result.reason_code == "EVIDENCE_SOURCE_FAILURE" and not thinking.calls
    assert source.reads == ["m1"] and "secret budget" not in result.detail
