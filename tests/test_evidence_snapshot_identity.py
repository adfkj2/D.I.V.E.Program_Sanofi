"""Admitted facts keep one identity across independent retrieval acquisitions."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from test_evidence_workspace import item, request

from dive_memory.answering import EvidenceCitation
from dive_memory.models import EvidenceState, MemoryStatus
from dive_memory.reading.snapshots import AdmittedSnapshot
from dive_memory.reading.workspace import BoundedEvidenceWorkspace


def workspace(*, budget=2000, policy="admitted-v1"):
    return BoundedEvidenceWorkspace(request(budget=budget), identity_policy=policy)


def test_legacy_identity_records_rank_and_channel_changes_as_distinct_documents():
    original = item("The budget is 37.")
    legacy = BoundedEvidenceWorkspace(request())
    legacy.add([original, replace(original, score=0.2, channels=["dense"])])
    assert legacy.pack()[1]["registered_documents"] == 2


def test_retrieval_rank_and_channels_do_not_duplicate_an_admitted_snapshot():
    original = item("The budget is 37.")
    pool = workspace()
    first = pool.add([original])
    second = pool.add([replace(original, score=0.2, channels=["dense", "bm25"])])
    visible, audit = pool.pack()
    assert len(visible) == audit["registered_documents"] == 1
    assert first["added_document_ids"] == second["duplicate_document_ids"]
    assert not second["added_document_ids"]
    assert audit["identity_policy"] == "admitted-v1"
    assert visible[0].score == 1 and visible[0].channels == ["lexical"]


def test_source_and_stamp_order_is_set_semantics_but_conflicting_stamps_remain():
    original = item("Budget approved.", refs=("event-b", "event-a"))
    original.memory.source_event_ids = ["event-b", "event-a"]
    stamps = [
        {
            "source_ref": "event-b",
            "role": "assistant",
            "statement_at": "2026-01-01T00:00:00Z",
        },
        {
            "source_ref": "event-a",
            "role": "user",
            "statement_at": "2026-01-02T00:00:00Z",
        },
    ]
    original.memory.structured_content["_reader_statement_sources"] = stamps
    reversed_item = deepcopy(original)
    reversed_item.source_refs.reverse()
    reversed_item.memory.source_event_ids.reverse()
    reversed_item.memory.structured_content["_reader_statement_sources"] = list(
        reversed(stamps)
    )
    pool = workspace()
    first = pool.add([original])
    duplicate = pool.add([reversed_item])
    assert duplicate["duplicate_document_ids"] == first["added_document_ids"]
    conflicting = deepcopy(original)
    conflicting.memory.structured_content["_reader_statement_sources"].append(
        {
            "source_ref": "event-a",
            "role": "assistant",
            "statement_at": "2026-01-02T00:00:00Z",
        }
    )
    pool.add([conflicting])
    assert pool.pack()[1]["registered_documents"] == 2


def test_reserved_retrieval_metadata_does_not_create_a_new_semantic_document():
    original = item("The budget is 37.")
    decorated = deepcopy(original)
    decorated.memory.structured_content["_retrieval"] = {
        "score": 0.5,
        "channels": ["dense"],
        "access_count": 19,
        "last_access": "2026-02-01T00:00:00Z",
    }
    pool = workspace()
    first = pool.add([original])
    later = pool.add([decorated])
    assert later["duplicate_document_ids"] == first["added_document_ids"]


@pytest.mark.parametrize(
    "key,metadata",
    [
        (
            "_retrieval_metadata",
            {"access_count": 19, "last_accessed_at": "2026-02-01T00:00:00Z"},
        ),
        (
            "_reader_acquisition",
            {"retrieval_count": 8, "last_retrieved_at": "2026-02-01T00:00:00Z"},
        ),
        ("_meta", {"access_count": 19, "last_access": "2026-02-01T00:00:00Z"}),
        ("_last_access", "2026-02-01T00:00:00Z"),
        ("_access_count", 8),
    ],
)
def test_known_access_annotations_are_separate_and_audited(key, metadata):
    original = item("The budget is 37.")
    decorated = deepcopy(original)
    decorated.memory.structured_content[key] = metadata
    pool = workspace()
    first = pool.add([original])
    second = pool.add([decorated])
    assert second["duplicate_document_ids"] == first["added_document_ids"]
    assert second["acquisitions"][0]["decorations"][key] == metadata


def test_unknown_fields_inside_reserved_metadata_are_retained_conservatively():
    original = item("The budget is 37.")
    changed = deepcopy(original)
    changed.memory.structured_content["_retrieval"] = {
        "score": 0.2,
        "source_ref": "new-origin",
    }
    pool = workspace()
    pool.add([original, changed])
    assert pool.pack()[1]["registered_documents"] == 2


def test_source_position_order_does_not_hide_a_true_position_change():
    original = item("The budget is 37.", refs=("first", "second"))
    first = {"source_ref": "first", "role": "user", "session_index": 1, "turn_index": 2}
    second = {
        "source_ref": "second",
        "role": "assistant",
        "session_index": 1,
        "turn_index": 3,
    }
    original.memory.structured_content["_reader_sources"] = [first, second]
    reordered = deepcopy(original)
    reordered.memory.structured_content["_reader_sources"].reverse()
    moved = deepcopy(original)
    moved.memory.structured_content["_reader_sources"][0]["turn_index"] = 4
    pool = workspace()
    initial = pool.add([original])
    assert (
        pool.add([reordered])["duplicate_document_ids"] == initial["added_document_ids"]
    )
    pool.add([moved])
    assert pool.pack()[1]["registered_documents"] == 2


def test_snapshot_contains_no_live_mutable_references_and_cannot_be_reassigned():
    original = item("The budget is 37.")
    snapshot = AdmittedSnapshot.from_item(original)
    fingerprint = snapshot.fingerprint()
    original.memory.content = "Forged content."
    original.memory.structured_content["value"] = "forged"
    assert snapshot.fingerprint() == fingerprint
    assert "Forged content" not in snapshot.canonical_json
    with pytest.raises(FrozenInstanceError):
        snapshot.canonical_json = "replacement"


def test_domain_values_named_like_access_statistics_are_not_silently_discarded():
    original = item("Daily gate access count is recorded.")
    original.memory.structured_content["access_count"] = 2
    changed = deepcopy(original)
    changed.memory.structured_content["access_count"] = 3
    pool = workspace()
    pool.add([original, changed])
    assert pool.pack()[1]["registered_documents"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "another-memory"),
        ("namespace", "other-project"),
        ("content", "The budget is 38."),
        ("version", 2),
        ("status", MemoryStatus.SUPERSEDED),
        ("evidence_state", EvidenceState.INFERENCE),
        ("valid_from", "2026-01-02T00:00:00Z"),
        ("valid_to", "2026-02-01T00:00:00Z"),
        ("observed_at", "2026-01-03T00:00:00Z"),
        ("created_at", "2026-01-04T00:00:00Z"),
        ("updated_at", "2026-01-05T00:00:00Z"),
        ("supersedes_id", "predecessor"),
        ("contradicts_id", "alternative"),
        ("model_version", "extractor-model-2"),
        ("extractor_version", "extractor-v2"),
    ],
)
def test_semantic_scope_lifecycle_and_origin_changes_remain_distinct(field, value):
    original = item("The budget is 37.")
    pool = workspace()
    pool.add(
        [original, replace(original, memory=replace(original.memory, **{field: value}))]
    )
    assert pool.pack()[1]["registered_documents"] == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("role", "assistant"),
        ("statement_at", "2026-02-01T00:00:00Z"),
        ("source_ref", "different-event"),
        ("source_message_id", "different-message"),
    ],
)
def test_statement_role_clock_or_source_change_is_not_a_rank_only_duplicate(key, value):
    original = item("The budget is 37.")
    original.memory.structured_content["_reader_statement_sources"] = [
        {
            "source_ref": "source1",
            "role": "user",
            "statement_at": "2026-01-01T00:00:00Z",
        }
    ]
    changed = deepcopy(original)
    changed.memory.structured_content["_reader_statement_sources"][0][key] = value
    pool = workspace()
    pool.add([original, changed])
    assert pool.pack()[1]["registered_documents"] == 2


def test_true_source_reference_and_ordered_domain_sequence_changes_are_preserved():
    original = item("The budget is 37.")
    original.memory.structured_content["steps"] = ["submit", "approve"]
    reordered = deepcopy(original)
    reordered.memory.structured_content["steps"].reverse()
    pool = workspace()
    pool.add([original, replace(original, source_refs=["other-event"]), reordered])
    assert pool.pack()[1]["registered_documents"] == 3


def test_acquisitions_preserve_transport_metadata_without_rewriting_the_first_snapshot():
    original = item("The budget is 37.")
    pool = workspace()
    pool.add([original])
    searched = replace(original, score=0.25, channels=["dense"])
    second = pool.add([searched])
    original.memory.content = "Mutated input."
    searched.channels.append("mutated-channel")
    visible, audit = pool.pack()
    assert [a["batch"] for a in audit["acquisitions"]] == [0, 1]
    assert [a["score"] for a in audit["acquisitions"]] == [1, 0.25]
    assert audit["acquisitions"][1]["channels"] == ["dense"]
    assert (
        second["acquisitions"][0]["document_id"]
        == audit["registered"][0]["document_id"]
    )
    assert audit["acquisitions"][1]["duplicate"] is True
    assert visible[0].memory.content == "The budget is 37."
    audit["acquisitions"][0]["channels"].append("audit-mutation")
    second["acquisitions"][0]["channels"].append("return-mutation")
    visible[0].memory.content = "Mutated output."
    later, later_audit = pool.pack()
    assert later[0].memory.content == "The budget is 37."
    assert later_audit["acquisitions"][0]["channels"] == ["lexical"]
    assert later_audit["acquisitions"][1]["channels"] == ["dense"]


def test_pins_survive_reacquisition_and_current_index_changes_without_extra_source_slots():
    pool = workspace(budget=600)
    first = item("The first budget is 37.", "first", ("first-source",))
    second = item("The second budget is 29.", "second", ("second-source",))
    pool.add([first, second])
    visible, initial = pool.pack()
    second_index = next(
        i for i, value in enumerate(visible) if value.memory.id == "second"
    )
    pins = pool.anchor(
        [EvidenceCitation("second", "budget is 29", ("second-source",))],
        visible,
        indices=[second_index],
    )
    pool.add(
        [
            replace(second, score=0.01, channels=["dense"]),
            item("New budget detail.", "new", ("new-source",)),
        ]
    )
    final, audit = pool.pack(pins=pins)
    assert not audit["pin_errors"] and final[0].memory.id == "second"
    assert (
        audit["registered_documents"] == 3 and len(audit["registered_source_ids"]) == 3
    )
    before = next(x for x in initial["selected"] if x["memory_id"] == "second")
    after = next(x for x in audit["selected"] if x["memory_id"] == "second")
    assert before["document_id"] == after["document_id"]
    assert audit["context_tokens"] <= audit["token_budget"]


def test_original_admitted_item_can_be_anchored_after_only_its_rank_changes():
    original = item("The budget is 37.")
    pool = workspace()
    pool.add([original])
    decorated = replace(original, score=0.02, channels=["dense"])
    pins = pool.anchor(
        [EvidenceCitation("m1", "budget is 37", ("source1",))], [decorated]
    )
    assert not pool.pack(pins=pins)[1]["pin_errors"]


def test_mutated_visible_semantics_cannot_anchor_a_new_claim():
    pool = workspace()
    pool.add([item("The budget is 37.")])
    visible, _ = pool.pack()
    visible[0].memory.structured_content["role"] = "forged"
    pins = pool.anchor([EvidenceCitation("m1", "budget is 37", ("source1",))], visible)
    assert pool.pack(pins=pins)[1]["pin_errors"]


def test_source_fairness_retains_different_memory_ids_with_the_same_origin():
    pool = workspace()
    first = item("The budget is 37.", "extracted", ("same-event",))
    pool.add(
        [
            first,
            item("The budget is 37.", "document", ("same-event",)),
            item("Second source budget.", "other", ("second-event",)),
        ]
    )
    pool.add([replace(first, score=0.01, channels=["dense"])])
    visible, audit = pool.pack()
    assert audit["registered_documents"] == 3
    assert len(audit["registered_source_ids"]) == 2
    assert {tuple(x.source_refs) for x in visible[:2]} == {
        ("same-event",),
        ("second-event",),
    }


def test_explicit_legacy_policy_has_the_default_add_pack_bytes_and_no_new_audit_fields():
    original = item("The budget is 37.")
    implicit = BoundedEvidenceWorkspace(request())
    explicit = workspace(policy="retrieval-v1")
    assert json.dumps(implicit.add([original]), sort_keys=True) == json.dumps(
        explicit.add([original]), sort_keys=True
    )
    assert json.dumps(implicit.pack()[1], sort_keys=True) == json.dumps(
        explicit.pack()[1], sort_keys=True
    )
    assert "acquisitions" not in implicit.pack()[1]
    assert "identity_policy" not in implicit.pack()[1]


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "unknown-policy"])
def test_identity_policy_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="identity_policy"):
        workspace(policy=value)
