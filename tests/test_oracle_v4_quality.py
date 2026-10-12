"""Offline invariants, not claims that a real model can solve these fixtures."""
from dataclasses import replace
import json

from dive_memory.models import Memory, EvidenceState, RetrievalItem
from dive_memory.context import pack_context
from dive_memory.oracle.evidence import build_evidence_views, render_evidence_block
from dive_memory.oracle.reader import (OracleReaderRequest, build_reader_prompt, reader_policy,
                                      assert_prompt_is_clean, GroundedChatReader, ReaderSpec)
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.source_coordinates import attach_source_coordinates
from dive_memory.token_budget import ApproximateTokenCounter


def item(id="m1", content="I joined the club four weeks ago.", refs=("e1",)):
    return RetrievalItem(Memory(id, "ns", "semantic_fact", EvidenceState.FACT, content,
                               {"subject": "user", "predicate": "statement", "value": content},
                               valid_from="2025-05-01T12:00:00+00:00"), 1.0, ["bm25"], list(refs))


def request(items=None):
    return OracleReaderRequest("How long was I a member at the meeting?", "2025-05-01T12:00:00+00:00",
                               "2025-05-01T12:00:00+00:00", tuple(items or [item()]), "unused")


def test_v4_removes_record_date_arithmetic_but_retains_missing_premise_guard():
    old = build_reader_prompt(request(), policy=reader_policy("evidence_first"))
    new = build_reader_prompt(request(), policy=reader_policy("derived_evidence"))
    assert old["context"] == new["context"]
    assert old["temporal_scaffold"]["instruction"] != new["temporal_scaffold"]["instruction"]
    rules = " ".join(new["rules"])
    assert "not necessarily the events" in rules
    assert "subtract the two event offsets" in rules
    assert "one entity's duration or event time is missing" in rules
    assert "answer=null and citations=[]" in rules
    assert_prompt_is_clean(new)


def test_v3_remains_default_and_does_not_receive_new_metadata_or_rules():
    old = build_reader_prompt(request())
    assert old == build_reader_prompt(request(), policy=reader_policy("evidence_first"))
    assert "source_order=" not in old["context"]
    assert "Record timestamps date the conversation" not in " ".join(old["rules"])


def test_coordinates_preserve_verbatim_content_and_original_memory():
    original = item()
    enriched = attach_source_coordinates([original], {"e1": {
        "source_ref": "e1", "session_index": 1, "turn_index": 4, "role": "user"}})
    assert enriched[0].memory.content == original.memory.content
    assert "_reader_sources" not in original.memory.structured_content
    view = build_evidence_views(enriched, question="club")[0]
    assert view.role == "user"
    assert "s1:t4:user" in render_evidence_block([view])


def test_merged_provenances_keep_both_roles_and_turns():
    items = [item(refs=("e1",)), item(id="m2", refs=("e2",))]
    annotated = attach_source_coordinates(items, {
        "e1": {"source_ref": "e1", "session_index": 1, "turn_index": 1, "role": "user"},
        "e2": {"source_ref": "e2", "session_index": 1, "turn_index": 2, "role": "assistant"},
    })
    packed = pack_context(annotated)
    views = build_evidence_views(packed.visible_items, question="club")
    assert len(views) == 1 and views[0].role == "mixed"
    assert len(views[0].source_positions) == 2
    assert len(annotated[0].memory.structured_content["_reader_sources"]) == 1


def test_coordinates_count_toward_visible_budget():
    annotated = attach_source_coordinates([item()], {"e1": {
        "source_ref": "e1", "session_index": 1, "turn_index": 4, "role": "user"}})
    render = lambda items: render_evidence_block(build_evidence_views(items, question="club"))
    length = ApproximateTokenCounter().count(render(annotated))
    assert len(pack_context(annotated, length, render_for_budget=render).items) == 1
    assert not pack_context(annotated, length - 1, render_for_budget=render).items


def test_v4_does_not_weaken_citation_validation():
    client = ScriptedChatClient(responses=[json.dumps({"status": "ANSWER", "answer": "three weeks",
        "reason_code": "DERIVED_BY_TEMPORAL_ARITHMETIC", "citations": [
            {"memory_id": "m1", "quote": "a quote not in the source", "source_refs": ["e1"]}],
        "missing_requirements": []})])
    policy = reader_policy("derived_evidence")
    reader = GroundedChatReader(client, ReaderSpec("test", "test", prompt_version=policy.prompt_version), policy=policy)
    assert reader.generate(request()).status.value == "INVALID"
