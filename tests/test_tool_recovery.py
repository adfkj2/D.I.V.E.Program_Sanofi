"""Synthetic recovery flows; no benchmark answers or paid model requests."""

import json
from dataclasses import replace

import pytest
from test_adaptive_reader import Source, final, item
from test_adaptive_reader import request as budget_request

from dive_memory.reading.adaptive import VERIFIED_VERSION, AdaptiveEvidenceReader
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ReaderSpec


def request(items=()):
    return replace(budget_request(items), question="What is the total budget?")


def verified(responses, source, max_calls=4):
    client = ScriptedChatClient(
        [json.dumps(r) for r in responses],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    reader = AdaptiveEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERIFIED_VERSION),
        source=source,
        verified_tools=True,
        max_calls=max_calls,
    )
    return reader, client


def payload(client, index):
    return json.loads(client.prompts[index].rsplit("\n", 1)[-1])


def calculation():
    return {
        "action": "calculate",
        "operation": "sum",
        "unit": "",
        "operands": [
            {"index": 0, "quote": "The budget is 20.", "label": "budget", "value": "20"}
        ],
    }


def test_missing_unit_is_rejected_then_model_may_correct_within_original_budget():
    action = calculation()
    missing = {k: v for k, v in action.items() if k != "unit"}
    source = Source([item("The budget is 20.")])
    r, client = verified([missing, action, final()], source)
    result = r.generate(request(source.items))
    assert result.status.value == "ANSWER" and result.calls == 3
    assert result.usage.input_tokens == 30 and result.usage.output_tokens == 15
    assert "unit" in payload(client, 1)["validation_error"]
    assert payload(client, 1)["calculation"] is None
    assert payload(client, 2)["calculation"]["answer"] == "20"
    assert source.searches == source.reads == []
    turns = json.loads(result.detail)["turns"]
    assert "calculation" not in turns[0] and turns[0]["validation_error"]


@pytest.mark.parametrize(
    "missing", [{"action": "search"}, {"action": "read", "memory_id": "m1"}]
)
def test_missing_read_or_search_field_is_reported_without_invoking_source(missing):
    source = Source([item("The budget is 20.")])
    r, client = verified([missing, final()], source)
    result = r.generate(request(source.items))
    assert result.status.value == "ANSWER" and result.calls == 2
    assert payload(client, 1)["validation_error"]
    assert source.searches == source.reads == []


@pytest.mark.parametrize(
    "action",
    [
        {"action": "search", "query": "budget"},
        {"action": "read", "memory_id": "m1", "start": 0},
        calculation(),
    ],
)
def test_repeated_action_is_not_executed_again_but_final_answer_can_recover(action):
    source = Source([item("The budget is 20.")])
    r, client = verified([action, action, final()], source)
    result = r.generate(request(source.items))
    assert result.status.value == "ANSWER" and result.calls == 3
    assert "repeated" in payload(client, 2)["validation_error"]
    assert len(source.searches) + len(source.reads) <= 1
    assert len(payload(client, 2)["completed_actions"]) == 1


def test_duplicate_query_with_outer_whitespace_cannot_execute_twice():
    source = Source([item("The budget is 20.")])
    r, client = verified(
        [
            {"action": "search", "query": "budget"},
            {"action": "search", "query": " budget "},
            final(),
        ],
        source,
    )
    assert r.generate(request()).status.value == "ANSWER"
    assert source.searches == ["budget"]
    assert "repeated" in payload(client, 2)["validation_error"]


def test_unrepaired_missing_fields_exhaust_budget_without_fabricated_abstention():
    source = Source([])
    r, client = verified([{"action": "search"}] * 4, source)
    result = r.generate(request())
    assert result.status.value == "INVALID" and result.hypothesis is None
    assert result.calls == len(client.prompts) == 4
    assert result.usage.input_tokens == 40
    assert source.searches == []
    assert payload(client, 3)["tools_available"] is False


def test_repeated_search_can_switch_query_and_still_finalize_in_four_calls():
    source = Source([item("The budget is 20.")])
    action = {"action": "search", "query": "budget"}
    r, client = verified(
        [action, action, {"action": "search", "query": "current budget"}, final()],
        source,
    )
    result = r.generate(request())
    assert result.status.value == "ANSWER" and result.calls == 4
    assert source.searches == ["budget", "current budget"]
    assert payload(client, 3)["validation_error"] is None


@pytest.mark.parametrize("operation", ["search", "read"])
@pytest.mark.parametrize("error", [ValueError, TypeError, RuntimeError])
def test_source_faults_are_not_treated_as_repairable_model_errors(operation, error):
    class Broken(Source):
        def search(self, query):
            self.searches.append(query)
            raise error("storage failure")

        def read(self, memory_id):
            self.reads.append(memory_id)
            raise error("storage failure")

    source = Broken([item("The budget is 20.")])
    action = (
        {"action": "search", "query": "budget"}
        if operation == "search"
        else {"action": "read", "memory_id": "m1", "start": 0}
    )
    r, client = verified([action, final()], source)
    result = r.generate(request(source.items))
    assert result.status.value == "ERROR" and result.calls == len(client.prompts) == 1
    assert result.error_type == "EVIDENCE_SOURCE_FAILURE"


@pytest.mark.parametrize(
    "action",
    [
        {"action": "search", "query": "budget", "answer": "20"},
        {"action": "calculate", "operation": "sum", "operands": [], "answer": "20"},
        {"action": "read", "memory_id": "not-visible", "start": 0},
        {"action": "search", "query": 42},
        {"action": "search", "query": " "},
        {"action": "read", "memory_id": "m1", "start": True},
        {"action": "delete", "memory_id": "m1"},
        {"action": {"search": "budget"}},
    ],
)
def test_extra_fields_and_unknown_sources_still_stop_before_tool_access(action):
    source = Source([item("The budget is 20.")])
    r, _ = verified([action, final()], source)
    result = r.generate(request(source.items))
    assert result.status.value == "INVALID" and result.calls == 1
    assert source.searches == source.reads == []


def test_failed_new_calculation_cannot_leave_a_previous_success_visible():
    action = calculation()
    malformed = {k: v for k, v in action.items() if k != "unit"}
    source = Source([item("The budget is 20.")])
    r, client = verified([action, malformed, final()], source)
    assert r.generate(request(source.items)).status.value == "ANSWER"
    assert payload(client, 1)["calculation"]["answer"] == "20"
    assert payload(client, 2)["calculation"] is None


@pytest.mark.parametrize("action", [{"action": "search"}, calculation()])
def test_recovery_never_adds_a_call_when_no_budget_remains(action):
    source = Source([item("The budget is 20.")])
    r, client = verified([action, final()], source, max_calls=1)
    result = r.generate(request(source.items))
    assert result.status.value == "INVALID" and result.calls == 1
    assert result.hypothesis is None and len(client.prompts) == 1
    assert source.searches == source.reads == []


def test_invalid_operand_after_success_clears_result_without_relaxing_quote_check():
    action = calculation()
    invalid = {
        **action,
        "operands": [
            {"index": 0, "quote": "invented 99", "label": "invented", "value": "99"}
        ],
    }
    source = Source([item("The budget is 20.")])
    r, client = verified([action, invalid, final()], source)
    result = r.generate(request(source.items))
    assert result.status.value == "ANSWER" and result.calls == 3
    assert payload(client, 1)["calculation"]["answer"] == "20"
    assert payload(client, 2)["calculation"] is None
    assert payload(client, 2)["validation_error"]
