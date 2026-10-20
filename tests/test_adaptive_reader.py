import json
from dataclasses import replace

import pytest

from dive_memory.answering import AnswerStatus
from dive_memory.application.readers import create_reader
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.models import EvidenceState, Memory, RetrievalItem
from dive_memory.reading.adaptive import VERSION, AdaptiveEvidenceReader
from dive_memory.reading.indexed_reader import parse_indexed
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ReaderRequest, ReaderSpec
from dive_memory.retrieval_session import MemoryEvidenceSource


def item(text, memory_id="m1", refs=("event1",)):
    return RetrievalItem(
        Memory(
            memory_id,
            "project",
            "semantic_fact",
            EvidenceState.FACT,
            text,
            {"subject": "user", "predicate": "statement", "value": text},
        ),
        1.0,
        ["lexical"],
        list(refs),
    )


def final(quote="budget is 20", index=0):
    return {
        "status": "ANSWER",
        "answer": "20",
        "citations": [{"index": index, "quote": quote}],
        "missing": [],
    }


class Source:
    def __init__(self, items):
        self.items = items
        self.searches = []
        self.reads = []

    def search(self, query):
        self.searches.append(query)
        return self.items

    def read(self, memory_id):
        self.reads.append(memory_id)
        return next(i for i in self.items if i.memory.id == memory_id)


def reader(responses, source, **kwargs):
    client = ScriptedChatClient(
        [json.dumps(r) if not isinstance(r, str) else r for r in responses],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    return AdaptiveEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERSION),
        source=source,
        **kwargs,
    ), client


def request(items=(), budget=1500):
    return ReaderRequest(
        "What is the budget?", None, None, tuple(items), "", token_budget=budget
    )


def test_active_search_recovers_missing_evidence_with_bounded_prompts():
    source = Source([item("The budget is 20.")])
    r, client = reader([{"action": "search", "query": "budget"}, final()], source)
    result = r.generate(request())
    assert result.status.value == "ANSWER" and result.hypothesis == "20"
    assert result.calls == 2 and result.usage.input_tokens == 20
    assert source.searches == ["budget"]
    audit = json.loads(result.detail)
    assert all(t["context_tokens"] <= t["token_budget"] for t in audit["turns"])
    assert (
        "budget is 20"
        not in json.loads(client.prompts[0].rsplit("\n", 1)[-1])["context"]
    )
    assert (
        "budget is 20" in json.loads(client.prompts[1].rsplit("\n", 1)[-1])["context"]
    )


def test_read_recovers_only_requested_window_and_preserves_other_passage():
    original = item("Header. " + "irrelevant text. " * 80 + "The budget is 20.")
    visible = replace(original, memory=replace(original.memory, content="Header. "))
    start = original.memory.content.index("The budget")
    source = Source([original])
    r, client = reader(
        [{"action": "read", "memory_id": "m1", "start": start}, final()], source
    )
    result = r.generate(request([visible]))
    assert result.status.value == "ANSWER" and source.reads == ["m1"]
    context = json.loads(client.prompts[1].rsplit("\n", 1)[-1])["context"]
    assert (
        "budget is 20" in context
        and "Header." in context
        and "irrelevant text" not in context
    )


def test_fragment_quotes_and_provenance_must_match_the_same_visible_passage():
    first, second = item("First fact", refs=("a",)), item("Second fact", refs=("b",))
    decision = parse_indexed(json.dumps(final("First fact", 0)), [first, second])
    assert decision.status == AnswerStatus.ANSWER
    from dive_memory.answering import validate_grounded_decision

    forged = replace(
        decision, citations=(replace(decision.citations[0], source_refs=("b",)),)
    )
    assert (
        validate_grounded_decision(forged, [first, second]).reason_code
        == "UNSUPPORTED_READER_OUTPUT"
    )
    assert (
        parse_indexed(
            json.dumps(final("First fact Second fact")), [first, second]
        ).reason_code
        == "UNSUPPORTED_READER_OUTPUT"
    )


@pytest.mark.parametrize(
    "action",
    [
        {"action": "read", "memory_id": "unknown", "start": 0},
        {"action": "read", "memory_id": "m1", "start": True},
        {"action": "read", "memory_id": "m1", "start": -1},
        {"action": "read", "memory_id": "m1", "start": 99999},
        {"action": "search", "query": " "},
        {"action": "search", "query": "budget", "answer": "20"},
        '{"action":"search","query":"budget","query":"secret"}',
    ],
)
def test_invalid_tool_calls_fail_closed(action):
    source = Source([item("budget is 20")])
    r, _ = reader([action], source)
    result = r.generate(request(source.items))
    assert result.status.value == "INVALID" and result.origin is None


def test_repeat_and_tool_budget_exhaustion_are_failures_not_refusals():
    action = {"action": "search", "query": "budget"}
    source = Source([])
    r, _ = reader([action, action], source)
    assert r.generate(request()).status.value == "INVALID"
    assert source.searches == ["budget"]
    r, _ = reader([action], Source([]), max_calls=1)
    assert r.generate(request()).status.value == "INVALID"


def test_failure_keeps_attempted_call_and_unknown_usage():
    class Broken(ScriptedChatClient):
        def complete(self, **kwargs):
            raise TimeoutError("fixture")

    r = AdaptiveEvidenceReader(
        Broken(),
        ReaderSpec("fixture", "fixture", prompt_version=VERSION),
        source=Source([]),
    )
    result = r.generate(request())
    assert result.status.value == "TIMEOUT" and result.calls == 1
    assert result.usage.input_tokens is result.usage.output_tokens is None


@pytest.mark.parametrize(
    "invalid",
    [
        '{"status":"ANSWER","answer":20 or 30}',
        '{"action":"search","query":"budget"}\n{"status":"ABSTAIN"}',
        final("unseen invented passage"),
        {"status": "ANSWER", "answer": "20", "citations": [], "extra": True},
    ],
)
def test_model_can_regenerate_invalid_json_or_citation_within_same_call_cap(invalid):
    source = Source([item("The budget is 20.")])
    r, client = reader([invalid, final()], source, max_calls=2)
    result = r.generate(request(source.items))
    assert result.status.value == "ANSWER" and result.hypothesis == "20"
    assert result.calls == 2 and result.usage.input_tokens == 20
    assert source.searches == [] and source.reads == []
    audit = json.loads(result.detail)["turns"]
    assert audit[0]["validation_error"]
    assert audit[0]["response"] == (
        invalid if isinstance(invalid, str) else json.dumps(invalid)
    )
    retry = json.loads(client.prompts[1].rsplit("\n", 1)[-1])
    assert retry["validation_error"] and retry["calls_remaining_including_this"] == 1
    assert "unseen invented passage" not in retry["context"]


def test_repeated_malformed_outputs_exhaust_cap_without_becoming_abstentions():
    r, client = reader(["not JSON"] * 4, Source([]))
    result = r.generate(request())
    assert result.status.value == "INVALID" and result.origin is None
    assert result.calls == len(client.prompts) == 4
    assert all(t["validation_error"] for t in json.loads(result.detail)["turns"])


def test_action_protocol_examples_are_valid_json_and_require_waiting_for_tools():
    from dive_memory.reading.adaptive import ADAPTIVE_RULES

    examples = [
        json.loads(line) for line in ADAPTIVE_RULES.splitlines() if line.startswith("{")
    ]
    assert len(examples) == 4
    assert {x.get("action") or x.get("status") for x in examples} == {
        "search",
        "read",
        "ANSWER",
        "ABSTAIN",
    }
    assert "Wait for the next user message" in ADAPTIVE_RULES


def test_final_turn_exposes_only_final_answers_and_bounded_action_history():
    source = Source([item("The budget is 20.")])
    action = {"action": "search", "query": "budget"}
    r, client = reader([action, final()], source, max_calls=2)
    result = r.generate(request())
    assert result.status.value == "ANSWER"
    prompt, payload_text = client.prompts[-1].rsplit("\n", 1)
    payload = json.loads(payload_text)
    assert payload["completed_actions"] == [action]
    assert payload["tools_available"] is False
    assert '"action":"search"' not in prompt
    assert '"action":"read"' not in prompt
    assert "No tool requests are permitted" in prompt
    assert len(payload["completed_actions"]) < r.max_calls


def test_source_enforces_project_deletion_and_cutoff():
    app = MemoryApplication()
    try:
        app.record("project", ProjectRecord("budget", "20"), idempotency_key="one")
        selected = app.service.retrieve("project", "budget").items
        assert selected
        source = MemoryEvidenceSource(app.service, "project", initial=selected)
        assert source.read(selected[0].memory.id).memory.namespace == "project"
        with pytest.raises(ValueError, match="retrieved"):
            MemoryEvidenceSource(app.service, "project").read(selected[0].memory.id)
        with pytest.raises(ValueError, match="project"):
            MemoryEvidenceSource(app.service, "other", initial=selected).read(
                selected[0].memory.id
            )
        with pytest.raises(ValueError, match="cutoff"):
            MemoryEvidenceSource(
                app.service, "project", initial=selected, as_of="2000"
            ).read(selected[0].memory.id)
        app.forget("project", selected[0].memory.id)
        with pytest.raises(ValueError, match="unavailable"):
            source.read(selected[0].memory.id)
    finally:
        app.close()


def test_product_binds_a_fresh_source_and_no_label_input():
    client = ScriptedChatClient(
        [
            json.dumps(
                {
                    "status": "ABSTAIN",
                    "answer": None,
                    "citations": [],
                    "missing": ["fixture"],
                }
            )
        ]
    )
    r = create_reader(client, model="fixture", strategy="adaptive")
    app = MemoryApplication(reader=r)
    try:
        app.record("project", ProjectRecord("budget", "20"), idempotency_key="one")
        assert app.answer("project", "budget")["status"] == "ABSTAIN"
        assert r.source is None
        assert not {"answer", "question_type", "answer_session_ids"} & set(
            json.loads(client.prompts[0].rsplit("\n", 1)[-1])
        )
    finally:
        app.close()


def test_oracle_binds_source_per_case_and_counts_searches(tmp_path):
    from dive_memory.application.oracle_run import r40_pipeline
    from dive_memory.eval_adapters.longmemeval import LongMemEvalCase, LongMemEvalTurn
    from dive_memory.oracle.loader import OracleDataset
    from dive_memory.oracle.pipeline import run_case
    from dive_memory.oracle.runner import OracleRunner

    case = LongMemEvalCase(
        question_id="synthetic",
        question="What is the budget?",
        answer="GOLD_DO_NOT_READ",
        question_type="single-session-user",
        question_date="2025-01-03T00:00:00Z",
        evidence_session_ids=("s",),
        turns=(
            LongMemEvalTurn(
                session_id="s",
                session_occurrence=1,
                turn_index=0,
                role="user",
                content="Please remember the project budget is 20.",
                occurred_at="2025-01-01T00:00:00Z",
                has_answer=True,
                haystack_index=0,
            ),
        ),
    )
    client = ScriptedChatClient(
        [
            json.dumps({"action": "search", "query": "project budget"}),
            json.dumps(
                {
                    "status": "ABSTAIN",
                    "answer": None,
                    "citations": [],
                    "missing": ["fixture"],
                }
            ),
        ]
    )
    r = create_reader(client, model="fixture", strategy="adaptive")
    dataset = OracleDataset(tmp_path / "fixture.json", "synthetic", (case,), 0)
    OracleRunner(dataset, tmp_path / "run", r, pipeline_config=r40_pipeline())
    result = run_case(case, reader=r)
    assert result.record["calls"]["retrieval"] == 2
    assert result.record["calls"]["reader"] == 2
    assert "GOLD_DO_NOT_READ" not in "".join(client.prompts)
    assert r.source is None
