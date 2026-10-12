"""The reader must not regain source text excluded by the context budget."""

import json
import pytest

from dive_memory.context import pack_context
from dive_memory.models import (
    DOCUMENT_ASSERTION,
    DOCUMENT_PREDICATE,
    EvidenceState,
    Memory,
    RetrievalItem,
)
from dive_memory.eval_adapters.longmemeval import LongMemEvalCase, LongMemEvalTurn
from dive_memory.oracle.contracts import ReaderStatus
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.pipeline import PipelineConfig, run_case
from dive_memory.oracle.reader import (
    DeterministicReader,
    GroundedChatReader,
    OracleReaderRequest,
    ReaderPolicy,
    ReaderSpec,
    build_reader_prompt,
    reader_outcome,
)
from dive_memory.token_budget import CallableTokenCounter


def _document() -> RetrievalItem:
    content = (
        "FIRST_PAGE_ONLY_TOKEN. " + "Opening logistics and schedules. " * 38
        + "\n\n## Final choice\nThe cobalt battery powers the telescope."
    )
    memory = Memory(
        "doc-1", "user-1", "episode", EvidenceState.OBSERVATION, content,
        {"subject": "assistant", "predicate": DOCUMENT_PREDICATE,
         "assertion": DOCUMENT_ASSERTION, "role": "assistant"},
        source_event_ids=["source-1"],
    )
    return RetrievalItem(memory, 1.0, ["bm25"], ["source-1"])


def _render(items):
    return render_evidence_block(build_evidence_views(
        items, question="Which battery powers the telescope?",
    ))


def test_budget_applies_to_actual_annotated_reader_context_and_source_span():
    item = _document()
    counter = CallableTokenCounter("characters", len)
    packed = pack_context(
        [item], 330, token_counter=counter,
        content_caps={DOCUMENT_PREDICATE: 150},
        question="Which battery powers the telescope?",
        document_selection="query_excerpt",
        render_for_budget=_render,
    )

    assert len(packed.items) == len(packed.visible_items) == 1
    assert packed.items[0].memory.content == item.memory.content
    visible = packed.visible_items[0].memory.content
    assert "cobalt battery" in visible
    assert "FIRST_PAGE_ONLY_TOKEN" not in visible
    assert visible in item.memory.content
    assert packed.text == _render(packed.visible_items)
    assert packed.estimated_tokens == len(packed.text) <= 330
    excerpt = next(row for row in packed.decisions if row["decision"] == "TRUNCATE")
    assert item.memory.content[excerpt["visible_start"]:excerpt["visible_end"]] == visible

    request = OracleReaderRequest(
        "Which battery powers the telescope?", None, None,
        tuple(packed.visible_items), packed.text,
        token_budget=330, policy=ReaderPolicy.evidence_first(),
    )
    prompt = build_reader_prompt(request)
    assert prompt["context"] == packed.text
    assert "FIRST_PAGE_ONLY_TOKEN" not in prompt["context"]


def test_provider_reader_rejects_quote_from_hidden_document_tail():
    item = _document()
    packed = pack_context(
        [item], 1000, content_caps={DOCUMENT_PREDICATE: 150},
        question="What did the opening discuss?",
        render_for_budget=_render,
    )
    assert "Opening logistics" in item.memory.content
    assert "cobalt battery" not in packed.visible_items[0].memory.content
    response = json.dumps({
        "status": "ANSWER", "answer": "cobalt battery",
        "reason_code": "SUPPORTED",
        "citations": [{"memory_id": "doc-1", "quote": "cobalt battery",
                       "source_refs": ["source-1"]}],
        "missing_requirements": [],
    })
    reader = GroundedChatReader(
        ScriptedChatClient(responses=[response]),
        ReaderSpec(provider="scripted", model="scripted-chat-v1"),
        policy=ReaderPolicy.evidence_first(),
    )
    request = OracleReaderRequest(
        "What did the opening discuss?", None, None,
        tuple(packed.visible_items), packed.text,
        policy=ReaderPolicy.evidence_first(),
    )
    outcome = reader_outcome(reader, request)
    assert outcome.status is ReaderStatus.INVALID
    assert outcome.hypothesis is None
    assert outcome.reason_code == "UNSUPPORTED_READER_OUTPUT"


def test_indexed_span_is_the_exact_passage_shown_to_reader():
    item = _document()
    text = item.memory.content
    start = text.index("## Final choice")
    end = len(text)
    packed = pack_context(
        [item], 1000, content_caps={DOCUMENT_PREDICATE: 150},
        question="Which battery powers the telescope?",
        document_selection="query_excerpt",
        document_spans={item.memory.id: (start, end)},
        render_for_budget=_render,
    )

    assert packed.visible_items[0].memory.content == text[start:end]
    row = next(row for row in packed.decisions if row["decision"] == "TRUNCATE")
    assert row["selection_source"] == "indexed_chunk"
    assert (row["visible_start"], row["visible_end"]) == (start, end)


def test_query_excerpt_rejects_zero_document_cap():
    with pytest.raises(ValueError, match="document character cap"):
        PipelineConfig(document_selection_name="query_excerpt", scoring_policy_name="legacy")


def test_document_channel_router_uses_question_not_hidden_question_type():
    turn = LongMemEvalTurn(
        "s1", 1, 0, "user", "Please remember that I live in Chengdu.",
        "2024-01-01T00:00:00+00:00",
    )
    config = PipelineConfig(
        reader_policy_name="evidence_first", scoring_policy_name="balanced",
        document_chunk_search_name="assistant_cued",
    )
    cases = [
        LongMemEvalCase("q-cued", "What did you recommend for me?", "x",
                        "single-session-user", "2024-01-02T00:00:00+00:00", (turn,), ()),
        LongMemEvalCase("q-plain", "Where do I live?", "x",
                        "single-session-assistant", "2024-01-02T00:00:00+00:00", (turn,), ()),
    ]
    cued = run_case(cases[0], reader=DeterministicReader(), config=config).record
    plain = run_case(cases[1], reader=DeterministicReader(), config=config).record

    assert cued["document_chunk_search_enabled"] is True
    assert plain["document_chunk_search_enabled"] is False
    assert "document_chunk" in cued["retrieval"]["executed_channels"]
    assert "document_chunk" not in plain["retrieval"]["executed_channels"]
    assert cued["context_text"] == cued["reader_context_text"]
    assert plain["context_text"] == plain["reader_context_text"]
    assert cued["packing"]["estimated_tokens"] <= config.token_budget
    assert plain["packing"]["estimated_tokens"] <= config.token_budget
