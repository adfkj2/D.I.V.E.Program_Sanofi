"""Receipts inherit a validated decision, never an unverified source or rewrite."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_argument_protocol import Source, analysis, draft, plan, request, reviewed
from test_evidence_workspace import item
from test_source_provenance import admitted

from dive_memory.reading.arguments import (
    ARGUMENT_ANSWER_RULES,
    ARGUMENT_PLAN_RULES,
    ARGUMENT_PROTOCOL,
    ARGUMENT_RECEIPT_PROTOCOL,
    ARGUMENT_REVIEW_RULES,
    parse_draft,
    parse_review,
    resolve_argument_review_receipt,
)
from dive_memory.reading.deliberative import DeliberativeEvidenceReader
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ReaderRequest, ReaderSpec
from dive_memory.retrieval_session import MemoryEvidenceSource
from dive_memory.service import MemoryService

RECEIPT = {"review": {"action": "accept", "defects": []}}


def reader(
    responses, *, source=None, protocol=ARGUMENT_RECEIPT_PROTOCOL, callback=None
):
    class Client(ScriptedChatClient):
        def complete(self, **kwargs):
            result = super().complete(**kwargs)
            if callback is not None:
                callback(len(self.prompts))
            return result

    client = Client([json.dumps(response) for response in responses])
    result = DeliberativeEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=protocol.version),
        protocol=protocol,
        source=source or Source(),
        workspace_identity="admitted-v1",
        annotate_sources=True,
    )
    return result, client


def test_receipt_preserves_all_draft_citations_across_reordered_current_labels():
    index = item("Project index: approval is in the budget note.", "index", ("index",))
    approval = item(
        "The final approved budget is exactly 37 dollars.", "approval", ("approval",)
    )
    assignment = item("Mira leads this project.", "assignment", ("assignment",))
    previous = [index, approval, assignment]
    text = {**draft("exactly 37 dollars"), "source_indices": [2, 1, 0]}
    old = parse_draft(json.dumps(text), previous)
    current = [approval, index, assignment]
    resolved = resolve_argument_review_receipt(
        json.dumps(RECEIPT), old, current, [assignment, approval, index]
    )
    assert resolved is old
    assert [citation.memory_id for citation in resolved.citations] == [
        "assignment",
        "approval",
        "index",
    ]
    assert resolved.answer == "exactly 37 dollars"
    with pytest.raises(ValueError, match="unavailable"):
        resolve_argument_review_receipt(
            json.dumps(RECEIPT),
            old,
            [approval, assignment],
            [assignment, approval, index],
        )


@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "other"},
        {"version": 2},
        {"observed_at": "2026-02-01T00:00:00Z"},
        {"status": "DELETED"},
        {"source_event_ids": ["another-event"]},
        {
            "structured_content": {
                "_reader_statement_sources": [
                    {
                        "source_ref": "source1",
                        "role": "assistant",
                        "statement_at": "2026-02-01",
                    }
                ]
            }
        },
    ],
)
def test_receipt_rejects_changed_source_snapshot(change):
    original = item("The approved budget is 37.")
    old = parse_draft(json.dumps(draft()), [original])
    current = replace(original, memory=replace(original.memory, **change))
    with pytest.raises(ValueError, match="unavailable"):
        resolve_argument_review_receipt(json.dumps(RECEIPT), old, [current], [original])


def test_receipt_accepts_only_expanded_window_of_same_snapshot():
    original = item("The approved budget is 37.")
    old = parse_draft(json.dumps(draft()), [original])
    expanded = replace(
        original,
        memory=replace(
            original.memory,
            content="Project note. " + original.memory.content + " End.",
        ),
    )
    assert (
        resolve_argument_review_receipt(
            json.dumps(RECEIPT), old, [expanded], [original]
        )
        is old
    )
    for prior in (
        None,
        [],
        [replace(original, memory=replace(original.memory, id="wrong"))],
    ):
        with pytest.raises(ValueError):
            resolve_argument_review_receipt(json.dumps(RECEIPT), old, [expanded], prior)


def test_receipt_preserves_abstention_and_missing_without_manufacturing_citations():
    body = {
        "status": "ABSTAIN",
        "answer": None,
        "source_indices": [],
        "missing": ["final approved amount"],
        "analysis": {
            "scope": "project budget",
            "events": [],
            "answer_details": [],
            "uncertainties": ["approval absent"],
        },
    }
    old = parse_draft(json.dumps(body), [])
    assert resolve_argument_review_receipt(json.dumps(RECEIPT), old, [], []) is old
    assert old.missing_requirements == ("final approved amount",)


@pytest.mark.parametrize(
    "field,value",
    [
        ("answer", "999"),
        ("status", "ABSTAIN"),
        ("source_indices", [0]),
        ("missing", []),
        ("extra", True),
    ],
)
def test_accept_receipt_cannot_smuggle_decision_fields(field, value):
    original = item("The approved budget is 37.")
    old = parse_draft(json.dumps(draft()), [original])
    with pytest.raises(ValueError, match="cannot carry"):
        resolve_argument_review_receipt(
            json.dumps({**RECEIPT, field: value}), old, [original], [original]
        )


@pytest.mark.parametrize(
    "review",
    [
        {"action": "accept", "defects": [], "answer": "999"},
        {
            "action": "accept",
            "defects": [{"kind": "support", "reason": "x", "source_indices": [0]}],
        },
        {"action": "unknown", "defects": []},
    ],
)
def test_receipt_review_envelope_is_strict(review):
    original = item("The approved budget is 37.")
    old = parse_draft(json.dumps(draft()), [original])
    with pytest.raises(ValueError):
        resolve_argument_review_receipt(
            json.dumps({"review": review}), old, [original], [original]
        )


def test_revise_still_needs_complete_decision_specific_defect_and_real_source():
    original = item("The approved budget is 38, not the draft's 37.")
    old = parse_draft(json.dumps(draft()), [original])
    final = reviewed(
        "38",
        action="revise",
        defects=[
            {
                "kind": "arithmetic",
                "reason": "The source states 38 rather than 37.",
                "source_indices": [0],
            }
        ],
    )
    decision = resolve_argument_review_receipt(
        json.dumps(final), old, [original], [original]
    )
    assert decision.answer == "38" and decision is not old
    for invalid in (
        {"review": final["review"]},
        {**final, "review": {"action": "revise", "defects": []}},
        {
            **final,
            "review": {
                "action": "revise",
                "defects": [
                    {
                        "kind": "support",
                        "reason": "No supporting source",
                        "source_indices": [],
                    }
                ],
            },
        },
        {**final, "source_indices": [1]},
        {**final, "answer": "37"},
    ):
        with pytest.raises(ValueError):
            resolve_argument_review_receipt(
                json.dumps(invalid), old, [original], [original]
            )


def test_reader_receipt_has_no_wire_indices_and_inherits_full_qualifiers():
    evidence = item("The final approved budget is exactly 37 dollars.")
    subject, client = reader([plan(), draft("exactly 37 dollars"), RECEIPT])
    result = subject.generate(request([evidence]))
    assert result.status.value == "ANSWER" and result.hypothesis == "exactly 37 dollars"
    assert len(client.prompts) == 3
    review = json.loads(result.detail)["turns"][-1]
    assert review["wire_citation_indices"] == [] and "citation_indices" not in review
    assert review["review_resolution"] == "HOST_INHERITED_DRAFT"
    assert review["host_inherited_citations"] == list(result.citations)


def test_tool_gathering_receipt_keeps_navigation_and_reordered_draft_sources():
    index = item("Budget details are in the approval note.", "index", ("index",))
    approval = item(
        "The final approved budget is exactly 37 dollars.", "approval", ("approval",)
    )
    assignment = item("Mira leads this project.", "assignment", ("assignment",))
    synthesis = {**draft("exactly 37 dollars"), "source_indices": [2, 1, 0]}
    source = Source([index, approval, assignment])
    subject, _ = reader(
        [plan(indices=[0], queries=["project budget"]), synthesis, RECEIPT],
        source=source,
    )
    result = subject.generate(request([index]))
    assert result.status.value == "ANSWER" and result.hypothesis == "exactly 37 dollars"
    assert {citation["memory_id"] for citation in result.citations} == {
        "index",
        "approval",
        "assignment",
    }
    turns = json.loads(result.detail)["turns"]
    assert [citation["memory_id"] for citation in result.citations] == [
        turns[1]["visible_memory_ids"][index] for index in [2, 1, 0]
    ]
    assert len(turns[-1]["host_inherited_citations"]) == 3


def test_provider_cannot_mutate_prompt_items_in_place_during_final_revalidation():
    class LiveSource(Source):
        changed = False

        def annotate(self, items):
            if self.changed:
                for evidence in items:
                    evidence.memory.version += 1
            return tuple(items)

    source = LiveSource()

    def change(call):
        if call == 3:
            source.changed = True

    subject, client = reader([plan(), draft(), RECEIPT], source=source, callback=change)
    result = subject.generate(request([item("The approved budget is 37.")]))
    assert result.status.value == "ERROR" and result.hypothesis is None
    assert (
        result.reason_code == "EVIDENCE_CHANGED_DURING_REVIEW"
        and len(client.prompts) == 3
    )


@pytest.mark.parametrize("moment", [2, 3])
@pytest.mark.parametrize(
    "mutation", ["soft_forget", "hard_forget", "disable", "version"]
)
def test_real_source_revoked_during_draft_or_review_cannot_be_inherited(
    moment, mutation
):
    service = MemoryService()
    try:
        evidence = admitted(service)
        source = MemoryEvidenceSource(service, "project", initial=[evidence])

        def change(call):
            if call != moment:
                return
            if mutation.endswith("forget"):
                service.forget(evidence.memory.id, hard=mutation == "hard_forget")
            elif mutation == "disable":
                service.set_memory_enabled("project", False)
            else:
                service.store.db.execute(
                    "UPDATE memories SET version=version+1 WHERE id=?",
                    (evidence.memory.id,),
                )
                service.store.db.commit()

        synthesis = {
            **draft("Cedar"),
            "analysis": {
                **analysis(),
                "scope": "employer",
                "answer_details": ["Cedar"],
                "events": [
                    {**analysis()["events"][0], "claim": "The user works at Cedar."}
                ],
            },
        }
        subject, client = reader(
            [plan(), synthesis, RECEIPT, RECEIPT], source=source, callback=change
        )
        result = subject.generate(
            ReaderRequest(
                "Where do I work?", None, None, (evidence,), "", token_budget=3000
            )
        )
        assert result.is_failure and result.hypothesis is None
        assert result.reason_code == "EVIDENCE_SOURCE_FAILURE"
        assert len(client.prompts) == moment
    finally:
        service.store.close()


def test_old_v1_prompts_and_nonreceipt_contract_are_unchanged():
    for rules, expected in [
        (
            ARGUMENT_PLAN_RULES,
            "c8cfef2b7c0665ca88b023ac141d8a4d6477033e791f9df0d3764f55bae7e018",
        ),
        (
            ARGUMENT_ANSWER_RULES,
            "69593ae5999c4b4021f9676b2fbbe43c23c5ef98bc4179c5e392d2fe688e961b",
        ),
        (
            ARGUMENT_REVIEW_RULES,
            "db2ceb2dff5cf56be4724ea5e3c775ddd80e3ac4498d507cfcbef8ee50a11f34",
        ),
    ]:
        assert hashlib.sha256(rules.encode()).hexdigest() == expected
    assert (
        ARGUMENT_PROTOCOL.resolve_review is None
        and not ARGUMENT_PROTOCOL.refresh_review_sources
    )
    with pytest.raises(ValueError):
        parse_review(json.dumps(RECEIPT), [item("The budget is 37.")])
    subject, _ = reader([plan(), draft(), reviewed()], protocol=ARGUMENT_PROTOCOL)
    result = subject.generate(request([item("The budget is 37.")]))
    assert result.status.value == "ANSWER"
    final = json.loads(result.detail)["turns"][-1]
    assert final["citation_indices"] == [0]
    assert (
        "wire_citation_indices" not in final and "host_inherited_citations" not in final
    )
