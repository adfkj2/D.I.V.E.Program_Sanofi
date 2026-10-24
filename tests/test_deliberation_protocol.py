import json

import pytest

from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.reading.deliberation import parse_decision, parse_plan


def item(text, memory_id="m1", refs=("event1",)):
    return RetrievalItem(
        Memory(memory_id, "project", "semantic_fact", EvidenceState.FACT, text, {}),
        1.0,
        ["lexical"],
        list(refs),
    )


def test_notes_require_the_exact_indexed_passage_not_another_fragment():
    items = [item("A budget was discussed."), item("The budget is 73.")]
    payload = {
        "notes": [{"index": 0, "quote": "budget is 73"}],
        "queries": [],
        "reads": [],
    }
    with pytest.raises(ValueError, match="exact visible quote"):
        parse_plan(json.dumps(payload), items)
    payload["notes"][0]["index"] = 1
    assert parse_plan(json.dumps(payload), items).notes[0].quote == "budget is 73"


@pytest.mark.parametrize("first_refs", [("old",), ("new",)])
def test_answer_requires_quote_and_provenance_from_the_indexed_fragment(first_refs):
    items = [
        item("A budget was discussed.", refs=first_refs),
        item("The budget is 73.", refs=("new",)),
    ]
    payload = {
        "status": "ANSWER",
        "answer": "73",
        "citations": [{"index": 0, "quote": "budget is 73"}],
        "missing": [],
    }
    with pytest.raises(ValueError):
        parse_decision(json.dumps(payload), items)
    payload["citations"][0]["index"] = 1
    decision = parse_decision(json.dumps(payload), items)
    assert decision.citations[0].source_refs == ("new",)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "{}{}",
        '{"notes":[],"queries":[],"reads":[],"reads":[]}',
        '{"notes":[],"queries":[],"reads":[],"answer":"invented"}',
    ],
)
def test_plan_rejects_ambiguous_or_mixed_phase_responses(text):
    with pytest.raises(ValueError):
        parse_plan(text, [])


@pytest.mark.parametrize(
    "field,value",
    [
        ("notes", [{"index": True, "quote": "budget"}]),
        ("notes", [{"index": 0, "quote": "paraphrased"}]),
        ("notes", [{"index": 0, "quote": " "}]),
        ("notes", [{"index": 0, "quote": "budget", "fact": "73"}]),
        ("notes", [{"index": 0, "quote": "x" * 401}]),
        ("queries", ["budget", " BUDGET "]),
        ("queries", [" "]),
        ("queries", ["a", "b", "c"]),
        ("reads", [{"memory_id": "unknown", "start": 0}]),
        ("reads", [{"memory_id": "m1", "start": True}]),
        ("reads", [{"memory_id": "m1", "start": -1}]),
        ("reads", [{"memory_id": "m1", "start": 0}] * 2),
    ],
)
def test_plan_validates_all_actions_before_any_source_access(field, value):
    payload = {"notes": [], "queries": [], "reads": [], field: value}
    with pytest.raises(ValueError):
        parse_plan(json.dumps(payload), [item("The budget is 73.")])


def test_plan_does_not_accept_a_quote_without_source_provenance():
    with pytest.raises(ValueError, match="provenance"):
        parse_plan(
            json.dumps(
                {"notes": [{"index": 0, "quote": "budget"}], "queries": [], "reads": []}
            ),
            [item("budget", refs=())],
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ABSTAIN", "answer": None, "citations": [], "missing": []},
        {"status": "ABSTAIN", "answer": None, "citations": [], "missing": [" "]},
        {
            "status": "ANSWER",
            "answer": "73",
            "citations": [{"index": 0, "quote": "budget"}],
            "missing": ["amount"],
        },
        {
            "status": "ANSWER",
            "answer": "73",
            "citations": [{"index": False, "quote": "budget"}],
            "missing": [],
        },
    ],
)
def test_decision_cannot_hide_invalid_output_as_a_semantic_abstention(payload):
    with pytest.raises(ValueError):
        parse_decision(json.dumps(payload), [item("budget")])


def test_decision_rejects_duplicate_keys_and_nonverbatim_quotes():
    with pytest.raises(ValueError, match="duplicate"):
        parse_decision(
            '{"status":"ANSWER","status":"ABSTAIN","answer":null,"citations":[],"missing":["amount"]}',
            [],
        )
    with pytest.raises(ValueError):
        parse_decision(
            json.dumps(
                {
                    "status": "ANSWER",
                    "answer": "73",
                    "citations": [{"index": 0, "quote": "budget is 73"}],
                    "missing": [],
                }
            ),
            [item("budget")],
        )
