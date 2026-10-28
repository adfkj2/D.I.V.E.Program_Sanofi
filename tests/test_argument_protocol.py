"""Untrusted argument records never bypass source or review boundaries."""

import json
from dataclasses import replace

import pytest
from test_evidence_workspace import item

from dive_memory.application.readers import create_reader
from dive_memory.models import MemoryStatus
from dive_memory.reading.arguments import (
    ARGUMENT_PROTOCOL,
    VERSION,
    parse_draft,
    parse_plan,
    parse_review,
    plan_context,
    review_context,
    validate_review,
)
from dive_memory.reading.deliberative import DeliberativeEvidenceReader
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ReaderRequest, ReaderSpec


def analysis(index=0):
    return {
        "scope": "Approved budget for the named project",
        "events": [
            {
                "claim": "The approved budget is 37",
                "source_indices": [index],
                "status": "reported",
                "time": "unknown",
                "group": "approval",
            }
        ],
        "answer_details": ["37"],
        "uncertainties": [],
    }


def draft(answer="37", index=0):
    return {
        "status": "ANSWER",
        "answer": answer,
        "source_indices": [index],
        "missing": [],
        "analysis": analysis(index),
    }


def reviewed(answer="37", index=0, action="accept", defects=()):
    return {
        "status": "ANSWER",
        "answer": answer,
        "source_indices": [index],
        "missing": [],
        "review": {"action": action, "defects": list(defects)},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "other-project"),
        ("version", 2),
        ("observed_at", "2026-02-01T00:00:00Z"),
    ],
)
def test_untrusted_analysis_cannot_rebind_a_different_semantic_snapshot(field, value):
    original = item("The approved budget is 37.")
    changed = replace(original, memory=replace(original.memory, **{field: value}))
    context = review_context(json.dumps(draft()), [original], [changed])
    event = context["untrusted_evidence_analysis"]["events"][0]
    assert event["current_source_indices"] == []
    assert event["unavailable_source_memory_ids"] == ["m1"]


def test_an_unlabelled_support_defect_cannot_blindly_replace_an_answer_value():
    evidence = [item("The approved budget is 37.")]
    original = parse_draft(json.dumps(draft()), evidence)
    text = json.dumps(
        reviewed(
            "999",
            action="revise",
            defects=[
                {
                    "kind": "support",
                    "reason": "The old answer is unsupported",
                    "source_indices": [],
                }
            ],
        )
    )
    with pytest.raises(ValueError):
        changed = parse_review(text, evidence)
        validate_review(text, original, changed, evidence)


def contract():
    return {
        "target": "Named project budget",
        "answer_unit": "approved amount",
        "include": ["latest approval"],
        "exclude": ["other projects"],
        "question_premises": [],
        "required_details": ["amount"],
    }


def plan(*, indices=(), queries=(), reads=()):
    return {
        "source_indices": list(indices),
        "queries": list(queries),
        "reads": list(reads),
        "question_contract": contract(),
    }


def abstain(*, audit="review"):
    result = {
        "status": "ABSTAIN",
        "answer": None,
        "source_indices": [],
        "missing": ["approved amount"],
    }
    result[audit] = (
        {
            "action": "revise",
            "defects": [
                {
                    "kind": "support",
                    "reason": "No approved amount is visible",
                    "source_indices": [],
                }
            ],
        }
        if audit == "review"
        else {
            "scope": "project budget",
            "events": [],
            "answer_details": [],
            "uncertainties": ["amount unavailable"],
        }
    )
    return result


class Source:
    def __init__(self, items=()):
        self.items = tuple(items)
        self.searches = []
        self.reads = []

    def search(self, query):
        self.searches.append(query)
        return self.items

    def read(self, memory_id):
        self.reads.append(memory_id)
        return next(i for i in self.items if i.memory.id == memory_id)


def setup_reader(responses, *, source=None, protocol=ARGUMENT_PROTOCOL, **kwargs):
    client = ScriptedChatClient(
        [r if isinstance(r, str) else json.dumps(r) for r in responses],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    reader = DeliberativeEvidenceReader(
        client,
        ReaderSpec("fixture", "fixture", prompt_version=VERSION),
        source=source or Source(),
        protocol=protocol,
        workspace_identity="admitted-v1",
        **kwargs,
    )
    return reader, client


def request(items=()):
    return ReaderRequest(
        "What is the named project budget?",
        None,
        None,
        tuple(items),
        "",
        token_budget=1500,
    )


def payload(client, index):
    return json.loads(client.prompts[index].rsplit("\n", 1)[-1])


@pytest.mark.parametrize(
    "change",
    [
        {"status": MemoryStatus.DELETED},
        {"status": MemoryStatus.SUPERSEDED},
        {"valid_to": "2026-01-10T00:00:00Z"},
        {"source_event_ids": ["different-event"]},
        {
            "structured_content": {
                "_reader_sources": [
                    {"source_type": "assistant", "observed_at": "2026-01-01"}
                ]
            }
        },
        {
            "structured_content": {
                "_reader_sources": [
                    {"source_type": "user", "observed_at": "2026-02-01"}
                ]
            }
        },
    ],
)
def test_untrusted_analysis_does_not_rebind_role_clock_or_lifecycle_changes(change):
    original = item("The approved budget is 37.")
    changed = replace(original, memory=replace(original.memory, **change))
    context = review_context(json.dumps(draft()), [original], [changed])
    event = context["untrusted_evidence_analysis"]["events"][0]
    assert event["current_source_indices"] == []
    assert event["unavailable_source_memory_ids"] == ["m1"]


def test_analysis_rebinds_current_labels_but_does_not_copy_old_source_text():
    original = item("The approved budget is 37.")
    reranked = replace(original, score=0.1, channels=["semantic"])
    unrelated = item("Another project costs 900.", "m2", ("source2",))
    context = review_context(json.dumps(draft()), [original], [unrelated, reranked])
    event = context["untrusted_evidence_analysis"]["events"][0]
    assert event["current_source_indices"] == [1]
    assert event["unavailable_source_memory_ids"] == []
    assert "source_indices" not in event
    # The claim remains explicitly untrusted. A missing source is identified,
    # never copied back into the packet as a new passage.
    unavailable = review_context(json.dumps(draft()), [original], [unrelated])
    assert "The approved budget is 37." not in json.dumps(unavailable)


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "assistant"),
        ("statement_at", "2026-02-01T00:00:00Z"),
        ("source_message_id", "a-different-message"),
    ],
)
def test_one_changed_source_stamp_cannot_preserve_a_draft_snapshot(field, value):
    original = item("The approved budget is 37.")
    stamp = {
        "source_ref": "source1",
        "role": "user",
        "statement_at": "2026-01-01T00:00:00Z",
        "source_message_id": "original-message",
    }
    original = replace(
        original,
        memory=replace(
            original.memory,
            structured_content={
                **original.memory.structured_content,
                "_reader_statement_sources": [stamp],
            },
        ),
    )
    changed = replace(
        original,
        memory=replace(
            original.memory,
            structured_content={
                **original.memory.structured_content,
                "_reader_statement_sources": [{**stamp, field: value}],
            },
        ),
    )
    event = review_context(json.dumps(draft()), [original], [changed])[
        "untrusted_evidence_analysis"
    ]["events"][0]
    assert event["current_source_indices"] == []
    final = json.dumps(reviewed())
    with pytest.raises(ValueError, match="accept"):
        validate_review(
            final,
            parse_draft(json.dumps(draft()), [original]),
            parse_review(final, [changed]),
            [changed],
            [original],
        )


def test_expanded_visible_window_can_preserve_the_same_snapshot():
    original = item("The approved budget is 37.")
    expanded = replace(
        original,
        memory=replace(
            original.memory,
            content="Project note. " + original.memory.content + " Approved today.",
        ),
    )
    context = review_context(json.dumps(draft()), [original], [expanded])
    assert context["untrusted_evidence_analysis"]["events"][0][
        "current_source_indices"
    ] == [0]
    final = json.dumps(reviewed())
    validate_review(
        final,
        parse_draft(json.dumps(draft()), [original]),
        parse_review(final, [expanded]),
        [expanded],
        [original],
    )


@pytest.mark.parametrize(
    "field,value",
    [("namespace", "other-project"), ("version", 2), ("status", MemoryStatus.DELETED)],
)
def test_accept_cannot_transfer_identical_quote_to_a_different_snapshot(field, value):
    original = item("The approved budget is 37.")
    changed = replace(original, memory=replace(original.memory, **{field: value}))
    final = json.dumps(reviewed())
    with pytest.raises(ValueError, match="accept"):
        validate_review(
            final,
            parse_draft(json.dumps(draft()), [original]),
            parse_review(final, [changed]),
            [changed],
            [original],
        )


def test_accept_cannot_drop_an_old_premise_while_keeping_the_answer():
    first = item("The approved budget is 37.")
    second = item("Approval applies to the named project.", "m2", ("source2",))
    proposal = draft()
    proposal["source_indices"] = [0, 1]
    proposal["analysis"]["events"].append(
        {
            "claim": "Approval concerns the named project",
            "source_indices": [1],
            "status": "reported",
            "time": "unknown",
            "group": "approval",
        }
    )
    final = json.dumps(reviewed())
    with pytest.raises(ValueError, match="accept"):
        validate_review(
            final,
            parse_draft(json.dumps(proposal), [first, second]),
            parse_review(final, [first, second]),
            [first, second],
            [first, second],
        )


def test_accept_cannot_silently_remove_a_required_name_qualifier():
    evidence = [item("Approved: Project Vega expansion budget is 37.")]
    proposal = parse_draft(json.dumps(draft("Project Vega expansion: 37")), evidence)
    final = json.dumps(reviewed("Project Vega: 37"))
    with pytest.raises(ValueError, match="accept"):
        validate_review(
            final, proposal, parse_review(final, evidence), evidence, evidence
        )


def test_missing_support_can_explain_abstention_without_fabricated_source():
    evidence = [item("A budget proposal is pending.")]
    proposal = parse_draft(json.dumps(draft()), evidence)
    final = json.dumps(abstain())
    decision = parse_review(final, evidence)
    validate_review(final, proposal, decision, evidence, evidence)
    assert decision.status.value == "ABSTAIN"


def test_revise_requires_an_actual_change_even_with_a_real_defect_label():
    evidence = [item("The approved budget is 37.")]
    proposal = parse_draft(json.dumps(draft()), evidence)
    final = json.dumps(
        reviewed(
            action="revise",
            defects=[
                {
                    "kind": "qualifier",
                    "reason": "Check the project name",
                    "source_indices": [0],
                }
            ],
        )
    )
    with pytest.raises(ValueError, match="actual change"):
        validate_review(
            final, proposal, parse_review(final, evidence), evidence, evidence
        )


@pytest.mark.parametrize("location", ["contract", "analysis", "defect"])
def test_unseen_sources_and_unbounded_audit_fields_are_rejected(location):
    evidence = [item("The approved budget is 37.")]
    if location == "contract":
        obj = plan()
        obj["question_contract"]["include"] = ["x"] * 7
        parser = parse_plan
    elif location == "analysis":
        obj = draft()
        obj["analysis"]["events"][0]["source_indices"] = [1]
        parser = parse_draft
    else:
        obj = reviewed(
            action="revise",
            defects=[
                {"kind": "scope", "reason": "Outside project", "source_indices": [1]}
            ],
        )
        parser = parse_review
    with pytest.raises(ValueError):
        parser(json.dumps(obj), evidence)


def test_question_contract_is_bounded_untrusted_context():
    text = json.dumps(plan())
    assert plan_context(text) == {"untrusted_question_contract": contract()}
    assert parse_plan(text, []).notes == ()


def test_actual_reader_searches_and_completes_all_three_audited_phases():
    source = Source([item("The approved budget is 37.")])
    reader, client = setup_reader(
        [plan(queries=["named project approved budget"]), draft(), reviewed()],
        source=source,
    )
    result = reader.generate(request())
    assert result.status.value == "ANSWER" and result.hypothesis == "37"
    assert result.calls == 3 and source.searches == ["named project approved budget"]
    assert result.usage.input_tokens == 30 and result.usage.output_tokens == 15
    assert "untrusted_question_contract" in payload(client, 1)
    assert "untrusted_evidence_analysis" in payload(client, 2)
    assert payload(client, 2)["untrusted_draft"]["answer"] == "37"
    assert "analysis" not in payload(client, 2)["untrusted_draft"]
    assert [t["phase"] for t in json.loads(result.detail)["turns"]] == [
        "plan",
        "synthesize",
        "review",
    ]


def test_actual_reader_passes_exact_cited_draft_snapshots_in_citation_order():
    observations = []

    def inspect(text, proposal, final, current, prior_items):
        observations.append(tuple(prior_items))
        validate_review(text, proposal, final, current, prior_items)

    first = item("Context only.", "m0", ("context",))
    second = item("The approved budget is 37.", "m1")
    reader, _ = setup_reader(
        [plan(), draft(index=1), reviewed(index=0)],
        protocol=replace(ARGUMENT_PROTOCOL, validate_review=inspect),
    )
    result = reader.generate(request([first, second]))
    assert result.status.value == "ANSWER"
    assert len(observations) == 1
    assert len(observations[0]) == 1 and observations[0][0].memory.id == "m1"
    assert observations[0][0].memory.namespace == "project"


def test_actual_reader_repairs_a_silent_accept_rewrite_once_then_finishes():
    reader, client = setup_reader(
        [
            plan(),
            draft("Project Vega expansion: 37"),
            reviewed("Project Vega: 37"),
            reviewed("Project Vega expansion: 37"),
        ]
    )
    result = reader.generate(
        request([item("Project Vega expansion approved budget is 37.")])
    )
    assert (
        result.status.value == "ANSWER"
        and result.hypothesis == "Project Vega expansion: 37"
    )
    assert result.calls == 4
    assert "accept cannot rewrite" in payload(client, 3)["validation_error"]


def test_actual_reader_never_returns_a_draft_after_review_failures():
    invalid = reviewed(
        "999",
        action="revise",
        defects=[{"kind": "support", "reason": "Unsupported", "source_indices": []}],
    )
    reader, _ = setup_reader([plan(), draft(), invalid, invalid])
    result = reader.generate(request([item("The approved budget is 37.")]))
    assert result.status.value == "INVALID" and result.hypothesis is None
    assert result.calls == 4


def test_actual_reader_has_only_one_shared_repair_slot_across_phases():
    reader, client = setup_reader(["not JSON", plan(), draft(), reviewed("999")])
    result = reader.generate(request([item("The approved budget is 37.")]))
    assert result.status.value == "INVALID" and result.hypothesis is None
    assert result.calls == len(client.prompts) == 4
    assert [t["phase"] for t in json.loads(result.detail)["turns"]] == [
        "plan",
        "plan",
        "synthesize",
        "review",
    ]


def test_actual_reader_mandatory_review_applies_even_to_an_abstaining_draft():
    proposal = abstain(audit="analysis")
    accepted = abstain()
    accepted["review"] = {"action": "accept", "defects": []}
    reader, _ = setup_reader([plan(), proposal, accepted])
    result = reader.generate(request())
    assert result.status.value == "ABSTAIN" and result.calls == 3


def test_actual_reader_annotates_initial_search_and_read_before_model_visibility():
    class AnnotatingSource(Source):
        def __init__(self, items):
            super().__init__(items)
            self.annotation_calls = []

        def annotate(self, items):
            self.annotation_calls.append(tuple(i.memory.id for i in items))
            return tuple(
                replace(
                    i,
                    memory=replace(
                        i.memory,
                        structured_content={
                            **i.memory.structured_content,
                            "_reader_statement_sources": [
                                {
                                    "source_ref": i.source_refs[0],
                                    "role": "user",
                                    "statement_at": "2026-01-01T00:00:00Z",
                                }
                            ],
                        },
                    ),
                )
                for i in items
            )

    original = item("The approved budget is 37.")
    source = AnnotatingSource([original])
    reader, client = setup_reader(
        [
            plan(queries=["budget"], reads=[{"memory_id": "m1", "start": 0}]),
            draft(),
            reviewed(),
        ],
        source=source,
        annotate_sources=True,
    )
    result = reader.generate(request([original]))
    assert result.status.value == "ANSWER"
    assert source.annotation_calls == [("m1",), ("m1",), ("m1",)]
    assert source.searches == ["budget"] and source.reads == ["m1"]
    assert all("statement_at" in payload(client, i)["context"] for i in range(3))


def test_actual_reader_surfaces_initial_provenance_failure_before_any_model_call():
    class FailingSource(Source):
        def annotate(self, items):
            raise ValueError("stale or unavailable snapshot")

    reader, client = setup_reader([], source=FailingSource(), annotate_sources=True)
    result = reader.generate(request([item("The approved budget is 37.")]))
    assert (
        result.status.value == "ERROR"
        and result.reason_code == "EVIDENCE_SOURCE_FAILURE"
    )
    assert result.calls == 0 and not client.prompts


def test_new_factory_strategy_binds_all_grounding_options_and_phase_transports():
    normal = ScriptedChatClient(
        [json.dumps(plan())], input_tokens_per_call=10, output_tokens_per_call=5
    )
    thinking = ScriptedChatClient(
        [json.dumps(draft()), json.dumps(reviewed())],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    reader = create_reader(
        normal, thinking, model="fixture", strategy="deliberative-grounded"
    )
    assert reader.version == VERSION
    result = reader.bind_evidence(Source()).generate(
        request([item("The approved budget is 37.")])
    )
    assert result.status.value == "ANSWER" and result.calls == 3
    assert len(normal.prompts) == 1 and len(thinking.prompts) == 2
    configuration = reader.execution_configuration()
    assert configuration["evidence_configuration"] == {
        "workspace_identity": "admitted-v1",
        "annotate_sources": True,
    }
    assert configuration["phases"]["plan"]["spec"]["max_tokens"] == 8192
    assert configuration["phases"]["synthesize"]["spec"]["max_tokens"] == 16384
    assert configuration["phases"]["review"]["spec"]["max_tokens"] == 16384
