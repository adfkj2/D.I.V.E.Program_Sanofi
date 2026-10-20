"""Synthetic development contracts, independent of benchmark IDs/answers."""

import json

import pytest
from test_adaptive_reader import Source, final, item

from dive_memory.application.oracle_run import main
from dive_memory.application.readers import create_reader
from dive_memory.contextual_fact_retention import ContextualFactRetention
from dive_memory.document_chunks import select_document_chunks
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.reading.adaptive import VERIFIED_VERSION, AdaptiveEvidenceReader
from dive_memory.reading.calculation import calculate
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ReaderRequest, ReaderSpec


@pytest.mark.parametrize(
    "text",
    [
        'I will give an example: our archive has only the final volume of "blue atlas".',
        "I am wondering if you can suggest a route to my sister Ada in Turin.",
        "I plan to visit my friends, who I met at a festival two months ago, and wondered if you know a hotel?",
        "My mother is 61 and my father is 64, so I want to set a good example.",
        "For example, I bought three notebooks last week.",
        'I finished "Copper River" yesterday. Can you recommend historical fiction?',
        "I can imagine travel being tiring. My parents visited Oslo last month.",
        "I was wondering if you knew a route to my cousin Elin in Bergen?",
    ],
)
def test_scoped_fact_survives_nonconditional_if_and_example(text):
    rows = ContextualFactRetention(scoped_assertions=True).candidates(
        text, source_type="user", observed_at=None, existing=[]
    )
    assert rows and rows[0].content == text
    assert rows[0].evidence_state == "OBSERVATION"
    assert text[slice(*rows[0].structured_content["source_span"])] == rows[0].content


@pytest.mark.parametrize(
    "text",
    [
        "If my mother is 61, what would you suggest?",
        "Imagine our archive has only the final volume of blue atlas.",
        "For a fictional example, I bought three notebooks last week.",
        'Translate this example: "I bought three notebooks last week."',
        "Suppose I bought three notebooks, then I own three notebooks.",
        "In my story, I visited Turin last week.",
        "I wish my father is 64.",
        "I am hoping to buy three notebooks tomorrow.",
    ],
)
def test_scoped_fact_keeps_hypotheses_quotes_and_plans_out(text):
    assert not ContextualFactRetention(scoped_assertions=True).candidates(
        text, source_type="user", observed_at=None, existing=[]
    )


def test_scoped_fact_rejects_inherited_hypothetical_scope_across_sentences():
    text = "Imagine this scenario. I bought three notebooks last week."
    assert not ContextualFactRetention(scoped_assertions=True).candidates(
        text, source_type="user", observed_at=None, existing=[]
    )


def test_disjoint_document_passages_preserve_both_sections_with_total_cap():
    text = (
        "Archive plan\nEquipment budget is 35.\n\n"
        + ("Routine administration and scheduling. " * 80)
        + "\nTravel budget is 65.\n"
    )
    chunks = select_document_chunks(text, "equipment travel budget", 600, max_chunks=2)
    assert len(chunks) == 2 and sum(len(c.content) for c in chunks) <= 600
    assert all(text[c.start : c.end] == c.content for c in chunks)
    assert any("Equipment budget is 35." in c.content for c in chunks)
    assert any("Travel budget is 65." in c.content for c in chunks)
    assert chunks[0].end <= chunks[1].start or chunks[1].end <= chunks[0].start


def test_calculation_rejects_same_scope_updates_even_across_different_sources():
    texts = ["I completed 3 courses on Cedar.", "I completed 5 courses on Cedar."]
    request = ReaderRequest(
        "How many courses in total?",
        None,
        None,
        tuple(item(t, f"m{i}", (f"e{i}",)) for i, t in enumerate(texts)),
        "",
    )
    plan = {
        "status": "CALCULATE",
        "operation": "sum",
        "unit": "courses",
        "operands": [
            {"index": i, "quote": t, "label": "Cedar", "value": str(n)}
            for i, (t, n) in enumerate(zip(texts, (3, 5)))
        ],
    }
    with pytest.raises(ValueError, match="scope"):
        calculate(plan, request, distinct_scopes=True)


def test_verified_reader_can_search_compute_and_answer_without_extra_calls():
    texts = ["I completed 3 courses on Cedar.", "I completed 5 courses on Maple."]
    items = [item(t, f"m{i}", (f"e{i}",)) for i, t in enumerate(texts)]
    responses = [
        {"action": "search", "query": "completed courses"},
        {
            "action": "calculate",
            "operation": "sum",
            "unit": "courses",
            "operands": [
                {"index": i, "quote": t, "label": label, "value": str(n)}
                for i, (t, label, n) in enumerate(
                    zip(texts, ("Cedar", "Maple"), (3, 5))
                )
            ],
        },
        {**final(texts[0]), "answer": "8 courses"},
    ]
    client = ScriptedChatClient([json.dumps(r) for r in responses])
    reader = AdaptiveEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERIFIED_VERSION),
        source=Source(items),
        verified_tools=True,
    )
    result = reader.generate(
        ReaderRequest("How many courses in total?", None, None, (), "")
    )
    assert result.status.value == "ANSWER" and result.hypothesis == "8 courses"
    assert result.calls == 3
    last = json.loads(client.prompts[-1].rsplit("\n", 1)[-1])
    assert last["calculation"]["answer"] == "8 courses"
    assert len(last["calculation"]["citations"]) == 2
    assert last["calculation"]["semantic_scope_verified"] is False


def test_verified_reader_calculation_error_does_not_become_answer():
    text = "The notebooks cost about 35 dollars."
    client = ScriptedChatClient(
        [
            json.dumps(r)
            for r in [
                {
                    "action": "calculate",
                    "operation": "sum",
                    "unit": "dollars",
                    "operands": [
                        {"index": 0, "quote": text, "label": "notebooks", "value": "35"}
                    ],
                },
                {
                    "status": "ABSTAIN",
                    "answer": None,
                    "citations": [],
                    "missing": ["exact cost"],
                },
            ]
        ]
    )
    reader = AdaptiveEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERIFIED_VERSION),
        source=Source([]),
        verified_tools=True,
    )
    result = reader.generate(
        ReaderRequest("What is the exact cost?", None, None, (item(text),), "")
    )
    assert result.status.value == "ABSTAIN" and result.calls == 2
    last = json.loads(client.prompts[-1].rsplit("\n", 1)[-1])
    assert last["calculation"] is None and "exact operand" in last["validation_error"]


@pytest.mark.parametrize("extra", [{"status": "ANSWER"}, {"answer": "999"}])
def test_calculation_action_rejects_unexpected_output_fields(extra):
    action = {
        "action": "calculate",
        "operation": "sum",
        "operands": [],
        "unit": "",
        **extra,
    }
    client = ScriptedChatClient([json.dumps(action)])
    reader = create_reader(client, model="fixture", strategy="verified").bind_evidence(
        Source([])
    )
    result = reader.generate(ReaderRequest("Total?", None, None, (), ""))
    assert result.status.value == "INVALID" and result.calls == 1


def test_calculation_cannot_execute_on_final_call():
    action = {"action": "calculate", "operation": "sum", "operands": [], "unit": ""}
    client = ScriptedChatClient([json.dumps(action)])
    reader = AdaptiveEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERIFIED_VERSION),
        source=Source([]),
        verified_tools=True,
        max_calls=1,
    )
    result = reader.generate(ReaderRequest("Total?", None, None, (), ""))
    assert result.status.value == "INVALID" and result.calls == 1
    assert "calculate tool is available" not in client.prompts[0]


def test_later_search_clears_previous_calculation():
    text = "The Cedar fee is 12."
    calculation = {
        "action": "calculate",
        "operation": "sum",
        "unit": "",
        "operands": [{"index": 0, "quote": text, "label": "Cedar", "value": "12"}],
    }
    client = ScriptedChatClient(
        [
            json.dumps(r)
            for r in [
                calculation,
                {"action": "search", "query": "updated fee"},
                {
                    "status": "ABSTAIN",
                    "answer": None,
                    "citations": [],
                    "missing": ["current fee"],
                },
            ]
        ]
    )
    reader = create_reader(client, model="fixture", strategy="verified").bind_evidence(
        Source([])
    )
    result = reader.generate(ReaderRequest("Total?", None, None, (item(text),), ""))
    assert result.status.value == "ABSTAIN"
    assert (
        json.loads(client.prompts[1].rsplit("\n", 1)[-1])["calculation"]["answer"]
        == "12"
    )
    assert json.loads(client.prompts[2].rsplit("\n", 1)[-1])["calculation"] is None


def test_scoped_configuration_is_explicit_and_reproducible(tmp_path, capsys):
    assert "scoped_fact_anchors" not in PipelineConfig().to_dict()
    with pytest.raises(ValueError, match="contextual retention"):
        PipelineConfig(scoped_fact_anchors=True)
    with pytest.raises(ValueError, match="boolean"):
        ContextualFactRetention(scoped_assertions="yes")
    assert (
        main(
            [
                "--dataset",
                str(tmp_path / "not-read-in-plan-mode.json"),
                "--output",
                str(tmp_path / "plan"),
                "--reader-strategy",
                "verified",
                "--fact-policy",
                "scoped",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert (
        plan["pipeline"]["scoped_fact_anchors"]
        and plan["reader_strategy"] == "verified"
    )
    assert plan["execution"] == "PLAN_ONLY"


@pytest.mark.parametrize(
    "max_chars,max_chunks", [(0, 2), (2, 4), (200, 0), (200, True)]
)
def test_passage_limits_reject_invalid_values(max_chars, max_chunks):
    with pytest.raises(ValueError):
        select_document_chunks("document", "query", max_chars, max_chunks=max_chunks)


def test_passage_no_match_and_short_documents_remain_bounded():
    assert select_document_chunks("", "budget", 20)[0].content == ""
    text = "This is a long passage. " * 20
    chunks = select_document_chunks(text, "unmatchedquery", 80)
    assert len(chunks) == 1 and chunks[0].start == 0 and len(chunks[0].content) <= 80
    assert select_document_chunks("Short report", "", 100)[0].content == "Short report"
