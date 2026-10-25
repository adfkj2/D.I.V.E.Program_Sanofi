import json

import pytest

from dive_memory.application.readers import create_reader
from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ReaderRequest
from dive_memory.reading.references import VERSION, parse_decision, parse_plan


def item(text, memory_id="m1", refs=("event1",)):
    return RetrievalItem(
        Memory(memory_id, "project", "semantic_fact", EvidenceState.FACT, text, {}),
        1.0,
        ["lexical"],
        list(refs),
    )


def plan(indices=(), **extra):
    return json.dumps(
        {"source_indices": list(indices), "queries": [], "reads": [], **extra}
    )


def decision(indices=(), **extra):
    return json.dumps(
        {
            "status": "ANSWER",
            "answer": "17",
            "source_indices": list(indices),
            "missing": [],
            **extra,
        }
    )


def test_selection_extracts_exact_visible_bytes_without_model_copying():
    text = "do we retain the U.S. label?\n预算为１７；名称：‘alpha’。  "
    result = parse_plan(plan([1]), [item("Other text."), item(text, "m2", ("event2",))])
    assert result.notes[0].quote == text
    assert result.notes[0].memory_id == "m2"
    assert result.notes[0].source_refs == ("event2",)


def test_same_memory_id_does_not_collapse_fragment_or_provenance():
    values = [
        item("The old limit was 12.", refs=("old",)),
        item("The limit is 17.", refs=("new",)),
    ]
    result = parse_decision(decision([1]), values)
    assert result.citations[0].quote == "The limit is 17."
    assert result.citations[0].source_refs == ("new",)


@pytest.mark.parametrize("indices", [[True], [-1], [1], ["0"], [0.0], [0, 0], None, {}])
def test_invalid_or_repeated_indices_are_not_repaired_by_the_parser(indices):
    payload = {"source_indices": indices, "queries": [], "reads": []}
    with pytest.raises(ValueError):
        parse_plan(json.dumps(payload), [item("The limit is 17.")])


@pytest.mark.parametrize(
    "value", [item("", refs=("event",)), item("  "), item("A fact.", refs=())]
)
def test_empty_or_unprovenanced_passages_cannot_be_selected(value):
    with pytest.raises(ValueError):
        parse_decision(decision([0]), [value])


@pytest.mark.parametrize(
    "payload",
    [
        '{"source_indices":[],"queries":[],"reads":[],"source_indices":[0]}',
        '{"source_indices":[0],"queries":[],"reads":[]} {}',
        '{"source_indices":[0],"quotes":["invented"],"queries":[],"reads":[]}',
    ],
)
def test_extra_fields_duplicate_keys_or_multiple_objects_are_rejected(payload):
    with pytest.raises(ValueError):
        parse_plan(payload, [item("The limit is 17.")])


def test_complete_visible_fragment_can_exceed_old_quote_copy_limit():
    text = "Actual source material. " * 25
    assert len(text) > 400
    assert parse_plan(plan([0]), [item(text)]).notes[0].quote == text


def test_plan_retains_the_same_search_and_read_boundaries():
    values = [item("The limit is 17.")]
    with pytest.raises(ValueError, match="visible memory"):
        parse_plan(plan([0], reads=[{"memory_id": "unseen", "start": 0}]), values)
    with pytest.raises(ValueError, match="repeated search"):
        parse_plan(plan([], queries=["limit", " LIMIT "]), values)
    with pytest.raises(ValueError, match="16"):
        parse_plan(plan(range(17)), [item(f"Source {i}.", str(i)) for i in range(17)])
    result = parse_plan(
        plan([0], queries=["limit"], reads=[{"memory_id": "m1", "start": 12}]), values
    )
    assert result.queries == ("limit",) and result.reads == (("m1", 12),)


def test_answer_needs_real_sources_and_abstention_needs_a_missing_premise():
    with pytest.raises(ValueError):
        parse_decision(decision(), [])
    with pytest.raises(ValueError):
        parse_decision(decision(status="ABSTAIN", answer=None), [])
    result = parse_decision(
        decision(
            status="ABSTAIN", answer=None, missing=["The new limit was not recorded."]
        ),
        [],
    )
    assert result.status.value == "ABSTAIN"


class Source:
    def search(self, query):
        return []

    def read(self, memory_id):
        raise AssertionError("No read was requested")


def test_factory_binding_preserves_three_phases_and_exact_quotes():
    text = "do keep this label: Project Z. The limit is 17."
    client = ScriptedChatClient([plan([0]), decision([0]), decision([0])])
    reader = create_reader(
        client, model="fixture", strategy="deliberative-refs"
    ).bind_evidence(Source())
    result = reader.generate(
        ReaderRequest(
            "What is the limit?", None, None, (item(text),), "", token_budget=1000
        )
    )
    assert result.hypothesis == "17" and result.calls == 3
    assert result.policy_version == reader.spec.prompt_version == VERSION
    assert result.citations[0]["quote"] == text
    audit = json.loads(result.detail)
    assert all(turn["citation_indices"] == [0] for turn in audit["turns"])
    assert all(
        packet["context_tokens"] <= packet["token_budget"]
        for packet in audit["workspace_packets"]
    )


def test_bad_source_selection_uses_shared_repair_then_stops_on_exhaustion():
    client = ScriptedChatClient([plan([9]), plan([9])])
    reader = create_reader(
        client, model="fixture", strategy="deliberative-refs"
    ).bind_evidence(Source())
    result = reader.generate(
        ReaderRequest(
            "What is the limit?",
            None,
            None,
            (item("The limit is 17."),),
            "",
            token_budget=1000,
        )
    )
    assert result.status.value == "INVALID" and result.calls == 2
    assert result.hypothesis is None


def test_one_bad_selection_can_be_corrected_without_reusing_failed_output():
    client = ScriptedChatClient([plan([9]), plan([0]), decision([0]), decision([0])])
    reader = create_reader(
        client, model="fixture", strategy="deliberative-refs"
    ).bind_evidence(Source())
    result = reader.generate(
        ReaderRequest(
            "What is the limit?",
            None,
            None,
            (item("The limit is 17."),),
            "",
            token_budget=1000,
        )
    )
    assert result.hypothesis == "17" and result.calls == 4
    audit = json.loads(result.detail)
    assert "outside current visible evidence" in audit["turns"][0]["validation_error"]
    assert audit["turns"][1]["citation_indices"] == [0]


def test_long_selected_passage_survives_search_within_the_evidence_budget():
    text = (
        "The recorded limit is 17. " + "Keep the associated procedure and scope. " * 12
    )

    class SearchSource(Source):
        def search(self, query):
            return [
                item(
                    "An unrelated background note. " * 15, f"other-{i}", (f"event-{i}",)
                )
                for i in range(8)
            ]

    client = ScriptedChatClient(
        [plan([0], queries=["procedure"]), decision([0]), decision([0])]
    )
    reader = create_reader(
        client, model="fixture", strategy="deliberative-refs"
    ).bind_evidence(SearchSource())
    result = reader.generate(
        ReaderRequest(
            "What is the limit?", None, None, (item(text),), "", token_budget=700
        )
    )
    assert result.hypothesis == "17" and result.citations[0]["quote"] == text
    audit = json.loads(result.detail)
    assert all(
        packet["context_tokens"] <= 700 and not packet["pin_errors"]
        for packet in audit["workspace_packets"]
    )
    assert text in audit["turns"][2]["context"]


def test_decision_does_not_trust_a_generated_quote_or_extra_field():
    with pytest.raises(ValueError, match="decision fields"):
        parse_decision(
            decision([0], quote="A fabricated claim."), [item("The limit is 17.")]
        )


def test_final_answer_may_reference_all_visible_premises():
    values = [item(f"Premise {i}.", str(i), (f"event-{i}",)) for i in range(17)]
    assert len(parse_decision(decision(range(17)), values).citations) == 17
