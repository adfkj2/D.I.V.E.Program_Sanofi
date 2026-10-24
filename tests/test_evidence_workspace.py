from copy import deepcopy
from dataclasses import replace

import pytest

from dive_memory.answering import EvidenceCitation
from dive_memory.models import EvidenceState, Memory, MemoryStatus, RetrievalItem
from dive_memory.reading.evidence import build_evidence_views, render_evidence_block
from dive_memory.reading.reader import ReaderRequest
from dive_memory.reading.workspace import BoundedEvidenceWorkspace
from dive_memory.token_budget import ApproximateTokenCounter


def item(text, memory_id="m1", refs=("source1",), **kwargs):
    return RetrievalItem(
        Memory(
            memory_id,
            "project",
            "semantic_fact",
            EvidenceState.FACT,
            text,
            {"subject": "user", "predicate": "statement", "value": text},
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            **kwargs,
        ),
        1.0,
        ["lexical"],
        list(refs),
    )


def request(*, question="What is the project budget?", budget=2000, items=()):
    return ReaderRequest(question, None, None, tuple(items), "", token_budget=budget)


def render(items, question="What is the project budget?"):
    return render_evidence_block(build_evidence_views(items, question=question))


def test_construction_does_not_implicitly_admit_request_items():
    workspace = BoundedEvidenceWorkspace(request(items=[item("Secret original.")]))
    items, audit = workspace.pack()
    assert not items
    assert audit["registered_documents"] == audit["context_tokens"] == 0


def test_repeated_identical_input_is_deduplicated_but_provenance_is_not():
    workspace = BoundedEvidenceWorkspace(request())
    first = item("A budget is 20.")
    added = workspace.add([first, deepcopy(first)])
    workspace.add([replace(first, source_refs=["different_source"])])
    visible, audit = workspace.pack()
    assert len(added["added_document_ids"]) == 1
    assert len(added["duplicate_document_ids"]) == 1
    assert audit["registered_documents"] == len(visible) == 2
    assert {tuple(i.source_refs) for i in visible} == {
        ("source1",),
        ("different_source",),
    }


def test_same_id_different_content_and_lifecycle_metadata_remain_distinct():
    workspace = BoundedEvidenceWorkspace(request())
    initial = item("Budget is 20.", valid_from="2026-01-01T00:00:00Z")
    newer = replace(
        initial,
        memory=replace(
            initial.memory,
            content="Budget is 25.",
            valid_from="2026-02-01T00:00:00Z",
        ),
    )
    archived = replace(
        initial, memory=replace(initial.memory, status=MemoryStatus.ARCHIVED)
    )
    workspace.add([initial, newer, archived])
    visible, audit = workspace.pack()
    assert audit["registered_documents"] == 3
    assert {
        (i.memory.content, i.memory.valid_from, i.memory.status) for i in visible
    } == {
        ("Budget is 20.", "2026-01-01T00:00:00Z", MemoryStatus.ACTIVE),
        ("Budget is 25.", "2026-02-01T00:00:00Z", MemoryStatus.ACTIVE),
        ("Budget is 20.", "2026-01-01T00:00:00Z", MemoryStatus.ARCHIVED),
    }


def test_input_and_output_mutations_cannot_rewrite_registered_evidence():
    workspace = BoundedEvidenceWorkspace(request())
    original = item("Budget is 20.")
    workspace.add([original])
    original.memory.content = "Injected replacement."
    original.memory.structured_content["subject"] = "attacker"
    original.source_refs.append("forged")
    visible, first = workspace.pack()
    visible[0].memory.content = "Returned object modified."
    visible[0].memory.structured_content["subject"] = "modified"
    visible[0].source_refs.append("modified")
    second_items, second = workspace.pack()
    assert second_items[0].memory.content == "Budget is 20."
    assert second_items[0].memory.structured_content["subject"] == "user"
    assert second_items[0].source_refs == ["source1"]
    assert first == second


def test_long_document_is_ranked_by_question_without_discarding_its_tail():
    text = "Unrelated details of a garden.\n" * 100 + "The project budget is 75."
    workspace = BoundedEvidenceWorkspace(request(budget=250), passage_chars=256)
    workspace.add([item(text)])
    visible, audit = workspace.pack()
    assert "budget is 75" in visible[0].memory.content
    assert all(len(i.memory.content) <= 256 for i in visible)
    assert audit["registered_fragments"] > len(visible)
    assert all(entry["reason"] == "context_budget" for entry in audit["omitted"])
    assert audit["context_tokens"] <= audit["token_budget"] == 250


def test_search_results_interleave_with_initial_sources_before_second_passages():
    workspace = BoundedEvidenceWorkspace(request(budget=6000), passage_chars=256)
    initial = [
        item("Budget first. " * 50, "initial-a", ("ia",)),
        item("Budget second. " * 50, "initial-b", ("ib",)),
    ]
    fresh = [
        item("Budget searched. " * 50, "new-a", ("na",)),
        item("Budget found. " * 50, "new-b", ("nb",)),
    ]
    workspace.add(initial)
    workspace.add(fresh)
    visible, audit = workspace.pack()
    assert [i.memory.id for i in visible[:4]] == [
        "initial-a",
        "new-a",
        "initial-b",
        "new-b",
    ]
    assert audit["selected"][0]["batch"] == 0
    assert audit["selected"][1]["batch"] == 1
    assert len(audit["visible_source_ids"]) == 4


def test_one_new_long_source_cannot_evict_all_initial_short_sources():
    workspace = BoundedEvidenceWorkspace(request(budget=500), passage_chars=256)
    workspace.add(
        [item("Budget first is 10.", "old-a"), item("Budget second is 15.", "old-b")]
    )
    workspace.add([item("Budget is a long discussion. " * 200, "new-long")])
    visible, audit = workspace.pack()
    assert {i.memory.id for i in visible} >= {"old-a", "old-b", "new-long"}
    assert audit["omitted"]


def test_all_registered_fragments_have_selection_or_omission_diagnostics():
    workspace = BoundedEvidenceWorkspace(request(budget=150), passage_chars=256)
    workspace.add([item("Budget notes. " * 200), item("", "empty")])
    _, audit = workspace.pack()
    assert audit["registered_fragments"] == len(audit["selected"]) + len(
        audit["omitted"]
    )
    assert len(audit["empty_document_ids"]) == 1
    assert len(audit["registered"]) == 2
    assert all(
        entry["offset_basis"] == "admitted_content" for entry in audit["omitted"]
    )


def test_fragment_identity_is_stable_when_context_indices_change():
    workspace = BoundedEvidenceWorkspace(request(budget=2000))
    workspace.add(
        [item("Budget one.", "a", ("event-a",)), item("Budget two.", "b", ("event-b",))]
    )
    _, first = workspace.pack()
    workspace.add([item("Budget three.", "c", ("event-c",))])
    _, second = workspace.pack()
    original = {entry["memory_id"]: entry for entry in first["selected"]}
    later = {entry["memory_id"]: entry for entry in second["selected"]}
    assert original["b"]["index"] != later["b"]["index"]
    assert original["b"]["fragment_id"] == later["b"]["fragment_id"]
    assert original["b"]["source_id"] == later["b"]["source_id"]


def test_multiple_memories_from_one_event_do_not_count_as_independent_sources():
    workspace = BoundedEvidenceWorkspace(request(budget=2000))
    workspace.add(
        [
            item("Budget draft is 20.", "extracted", ("event-a",)),
            item("Budget draft is 20, pending approval.", "original", ("event-a",)),
            item("Budget approved is 30.", "update", ("event-b",)),
        ]
    )
    visible, audit = workspace.pack()
    assert {tuple(i.source_refs) for i in visible[:2]} == {("event-a",), ("event-b",)}
    assert len(audit["registered_source_ids"]) == 2
    assert (
        len(visible) == 3
    )  # All distinct memories remain available, with their qualifiers.


def test_pinned_quote_crossing_fragment_boundary_is_an_exact_source_window():
    original = item("x" * 250 + "Specific budget is exactly 75 units." + "y" * 700)
    quote = original.memory.content[240:290]
    workspace = BoundedEvidenceWorkspace(request(budget=300), passage_chars=256)
    workspace.add([original])
    visible, audit = workspace.pack(pins=[EvidenceCitation("m1", quote, ("source1",))])
    assert not audit["pin_errors"]
    assert quote in visible[0].memory.content
    entry = audit["selected"][0]
    assert entry["reason"] == "pinned_citation"
    assert (
        visible[0].memory.content
        == original.memory.content[entry["start"] : entry["end"]]
    )


def test_pins_validate_source_provenance_instead_of_id_or_text_alone():
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("Budget is 20.")])
    invalid = [
        EvidenceCitation("unknown", "Budget", ("source1",)),
        EvidenceCitation("m1", "not present", ("source1",)),
        EvidenceCitation("m1", "Budget", ("wrong_source",)),
        EvidenceCitation("m1", "Budget", ()),
        EvidenceCitation("m1", "", ("source1",)),
        EvidenceCitation("m1", " ", ("source1",)),
    ]
    _, audit = workspace.pack(pins=invalid)
    assert len(audit["pin_errors"]) == len(invalid)
    assert {entry["reason"] for entry in audit["pin_errors"]} == {"pin_not_admitted"}


def test_correct_provenance_selects_matching_same_id_document():
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("The budget is 20.", refs=("source-old",))])
    workspace.add([item("The budget is 20.", refs=("source-new",))])
    visible, audit = workspace.pack(
        pins=[EvidenceCitation("m1", "budget is 20", ("source-new",))]
    )
    assert not audit["pin_errors"]
    assert visible[0].source_refs == ["source-new"]


def test_duplicate_and_overlapping_pin_windows_coalesce_without_losing_quotes():
    text = "A budget first is 10. A budget second is 15. " + "Neutral context. " * 40
    workspace = BoundedEvidenceWorkspace(request(budget=300), passage_chars=256)
    workspace.add([item(text)])
    citations = [
        EvidenceCitation("m1", "budget first is 10", ("source1",)),
        EvidenceCitation("m1", "budget first is 10", ("source1",)),
        EvidenceCitation("m1", "budget second is 15", ("source1",)),
    ]
    visible, audit = workspace.pack(pins=citations)
    assert not audit["pin_errors"]
    pinned = [
        entry for entry in audit["selected"] if entry["reason"] == "pinned_citation"
    ]
    assert len(pinned) == 1
    assert all(citation.quote in visible[0].memory.content for citation in citations)


def test_fully_covered_fragments_are_not_repeated_after_pinning():
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("The whole budget is 20.")])
    visible, audit = workspace.pack(
        pins=[EvidenceCitation("m1", "budget", ("source1",))]
    )
    assert len(visible) == 1
    assert audit["omitted"][0]["reason"] == "covered_by_pin"


def test_pin_larger_than_budget_is_reported_instead_of_silently_clipped():
    quote = "budget " * 100
    workspace = BoundedEvidenceWorkspace(request(budget=50))
    workspace.add([item(quote)])
    visible, audit = workspace.pack(pins=[EvidenceCitation("m1", quote, ("source1",))])
    assert not visible
    assert audit["pin_errors"][0]["reason"] == "pin_budget"
    assert audit["context_tokens"] <= 50


def test_long_exact_quote_is_never_truncated_to_the_normal_pin_window():
    quote = "A supported budget premise. " * 20
    workspace = BoundedEvidenceWorkspace(request(budget=1000), passage_chars=256)
    workspace.add([item("preface. " + quote + "ending.")])
    visible, audit = workspace.pack(pins=[EvidenceCitation("m1", quote, ("source1",))])
    assert quote in visible[0].memory.content
    assert not audit["pin_errors"]


def test_zero_budget_keeps_evidence_registered_and_reports_omission():
    workspace = BoundedEvidenceWorkspace(request(budget=0))
    workspace.add([item("A supported budget premise.")])
    visible, audit = workspace.pack()
    assert visible == ()
    assert audit["context_tokens"] == 0
    assert audit["registered_documents"] == 1
    assert audit["omitted"][0]["reason"] == "context_budget"


def test_full_provenance_and_temporal_metadata_survive_slicing():
    original = item(
        "Budget data. " * 100,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
        observed_at="2026-01-03T00:00:00Z",
        status=MemoryStatus.SUPERSEDED,
        contradicts_id="older",
        source_event_ids=["event-original"],
    )
    original.memory.structured_content["_reader_statement_sources"] = [
        {
            "source_ref": "source1",
            "role": "user",
            "statement_at": "2026-01-01T00:00:00Z",
        }
    ]
    workspace = BoundedEvidenceWorkspace(request(), passage_chars=256)
    workspace.add([original])
    visible, audit = workspace.pack()
    for fragment in visible:
        assert fragment.memory.structured_content == original.memory.structured_content
        assert (
            replace(fragment.memory, content=original.memory.content) == original.memory
        )
        assert fragment.source_refs == original.source_refs
        assert fragment.channels == original.channels
        assert fragment.score == original.score
    assert audit["context_tokens"] == ApproximateTokenCounter().count(render(visible))
    assert audit["token_counter_exact"] is False


def test_index_anchor_retains_the_seen_occurrence_of_a_repeated_quote():
    original = item(
        "January status\nBudget is 20.\n"
        + "Unrelated garden notes.\n" * 80
        + "February status\nBudget is 20.\n"
    )
    workspace = BoundedEvidenceWorkspace(
        request(question="What is the February budget?", budget=1000),
        passage_chars=256,
    )
    workspace.add([original])
    visible, _ = workspace.pack()
    index = next(
        i for i, value in enumerate(visible) if "February" in value.memory.content
    )
    pins = workspace.anchor(
        [EvidenceCitation("m1", "Budget is 20.", ("source1",))],
        visible,
        indices=[index],
    )
    reviewed, audit = workspace.pack(pins=pins)
    assert not audit["pin_errors"]
    assert "February" in reviewed[0].memory.content
    assert "January" not in reviewed[0].memory.content
    assert (
        audit["selected"][0]["start"] > original.memory.content.index("February") - 256
    )


def test_repeated_quote_within_one_visible_passage_retains_that_whole_passage():
    original = item("January: budget is 20. February: budget is 20.")
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([original])
    visible, _ = workspace.pack()
    pins = workspace.anchor(
        [EvidenceCitation("m1", "budget is 20", ("source1",))], visible
    )
    reviewed, audit = workspace.pack(pins=pins)
    assert not audit["pin_errors"]
    assert reviewed[0].memory.content == original.memory.content


def test_without_indices_all_matching_previously_visible_contexts_are_pinned():
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("January budget is 20."), item("February budget is 20.")])
    visible, _ = workspace.pack()
    pins = workspace.anchor(
        [EvidenceCitation("m1", "budget is 20", ("source1",))], visible
    )
    reviewed, audit = workspace.pack(pins=pins)
    assert len(pins) == 2
    assert not audit["pin_errors"]
    assert {i.memory.content for i in reviewed} == {
        "January budget is 20.",
        "February budget is 20.",
    }


def test_anchor_survives_later_packs_without_resolving_a_stale_index_again():
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("Initial budget is 20.")])
    original_visible, _ = workspace.pack()
    workspace.add([item("A different budget is 30.", "new")])
    workspace.pack()
    pins = workspace.anchor(
        [EvidenceCitation("m1", "budget is 20", ("source1",))], original_visible
    )
    reviewed, audit = workspace.pack(pins=pins)
    assert not audit["pin_errors"]
    assert reviewed[0].memory.id == "m1"
    assert "budget is 20" in reviewed[0].memory.content


def test_unregistered_or_mutated_visible_item_cannot_supply_a_pin_anchor():
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("The budget is 20.")])
    visible, _ = workspace.pack()
    visible[0].memory.structured_content["role"] = "forged"
    pins = workspace.anchor(
        [EvidenceCitation("m1", "budget is 20", ("source1",))], visible
    )
    _, audit = workspace.pack(pins=pins)
    assert audit["pin_errors"][0]["reason"] == "pin_visible_anchor_missing"


@pytest.mark.parametrize("index", [-1, 3, True])
def test_invalid_original_citation_index_is_an_explicit_anchor_error(index):
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([item("The budget is 20.")])
    visible, _ = workspace.pack()
    pins = workspace.anchor(
        [EvidenceCitation("m1", "budget is 20", ("source1",))],
        visible,
        indices=[index],
    )
    _, audit = workspace.pack(pins=pins)
    assert audit["pin_errors"][0]["reason"] == "pin_visible_anchor_missing"


def test_indices_must_correspond_to_every_citation():
    workspace = BoundedEvidenceWorkspace(request())
    with pytest.raises(ValueError, match="indices"):
        workspace.anchor(
            [EvidenceCitation("m1", "Budget", ("source1",))], (), indices=[]
        )


def test_original_admitted_item_can_be_anchored_before_any_pack():
    original = item("The budget is 20.")
    workspace = BoundedEvidenceWorkspace(request())
    workspace.add([original])
    pins = workspace.anchor(
        [EvidenceCitation("m1", "budget is 20", ("source1",))], [original]
    )
    visible, audit = workspace.pack(pins=pins)
    assert not audit["pin_errors"]
    assert visible[0].memory.content == original.memory.content


def test_pinned_old_claims_can_omit_new_counterevidence_but_never_hide_the_omission():
    first = item("Budget first was 20.", "first")
    second = item("Budget second was 30.", "second")
    correction = item("Correction: the total budget is 70.", "correction")
    budget = ApproximateTokenCounter().count(render([first, second]))
    workspace = BoundedEvidenceWorkspace(request(budget=budget))
    workspace.add([first, second])
    initial, _ = workspace.pack()
    pins = workspace.anchor(
        [
            EvidenceCitation("first", "Budget first was 20.", ("source1",)),
            EvidenceCitation("second", "Budget second was 30.", ("source1",)),
        ],
        initial,
    )
    workspace.add([correction])
    visible, audit = workspace.pack(pins=pins)
    assert not audit["pin_errors"]
    assert {i.memory.id for i in visible} == {"first", "second"}
    omitted = [
        entry for entry in audit["omitted"] if entry["memory_id"] == "correction"
    ]
    assert omitted and omitted[0]["reason"] == "context_budget"
    assert correction.source_refs == omitted[0]["source_refs"]


@pytest.mark.parametrize("size", [True, 0, 255, 4801, "800"])
def test_passage_size_rejects_invalid_resource_configuration(size):
    with pytest.raises(ValueError, match="passage_chars"):
        BoundedEvidenceWorkspace(request(), passage_chars=size)


@pytest.mark.parametrize("budget", [True, -1, "100"])
def test_budget_rejects_invalid_resource_configuration(budget):
    with pytest.raises(ValueError, match="token_budget"):
        BoundedEvidenceWorkspace(request(budget=budget))
