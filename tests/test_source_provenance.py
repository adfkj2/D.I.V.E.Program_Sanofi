"""Admitted-source metadata never widens a memory's text or observation scope."""

from dataclasses import FrozenInstanceError, replace

import pytest

from dive_memory.models import Event, MemoryStatus, RetrievalItem
from dive_memory.reading.evidence import build_evidence_views, render_evidence_block
from dive_memory.reading.provenance import MemoryServiceProvenanceProvider, SourceStamp
from dive_memory.retrieval_session import MemoryEvidenceSource
from dive_memory.service import MemoryService


@pytest.fixture
def service():
    instance = MemoryService()
    yield instance
    instance.store.close()


def admitted(service, *, role="user", observed="2026-03-14T10:00:00Z", occurred=None):
    result = service.ingest(
        "project",
        "I work at Cedar.",
        explicit=True,
        source_type=role,
        observed_at=observed,
        occurred_from=occurred,
        source_message_id="opaque:turn-42",
    )
    assert result["memory_ids"]
    memory = service.get_memory(result["memory_ids"][0])
    return RetrievalItem(memory, 0.75, ["dense"], list(memory.source_event_ids))


def extra_source(
    service,
    item,
    *,
    ref="extra",
    namespace="project",
    role="assistant",
    when="2026-03-14T11:00:00Z",
):
    service.store.append_event(
        Event(
            ref,
            namespace,
            "message",
            {"source_type": role, "text": "unused raw event body"},
            when,
            source_message_id="opaque-extra",
        )
    )
    service.store.add_memory_source(item.memory.id, ref)
    memory = service.get_memory(item.memory.id)
    return replace(item, memory=memory, source_refs=list(memory.source_event_ids))


def stamps(item):
    return item.memory.structured_content["_reader_statement_sources"]


def test_stamp_is_immutable_and_has_only_allowlisted_public_metadata():
    value = SourceStamp("event", "user", "2026-03-14T10:00:00Z", "opaque:42")
    assert value.to_dict() == {
        "source_ref": "event",
        "role": "user",
        "statement_at": "2026-03-14T10:00:00Z",
        "source_message_id": "opaque:42",
    }
    with pytest.raises(FrozenInstanceError):
        value.role = "assistant"


def test_initial_read_and_search_metadata_are_explicit_and_do_not_change_legacy_behavior(
    service,
):
    item = admitted(service)
    source = MemoryEvidenceSource(service, "project", initial=[item])
    assert (
        "_reader_statement_sources"
        not in source.read(item.memory.id).memory.structured_content
    )
    annotated = source.annotate([item])[0]
    assert stamps(annotated)[0]["role"] == "user"
    read = source.annotate([source.read(item.memory.id)])[0]
    searched = source.annotate(source.search("Cedar"))
    assert stamps(read) == stamps(annotated)
    assert any(stamps(found) == stamps(annotated) for found in searched)
    assert (
        "_reader_statement_sources"
        not in service.get_memory(item.memory.id).structured_content
    )


def test_statement_clock_uses_observed_time_not_event_or_valid_time(service):
    item = admitted(service, occurred="2026-02-03T08:00:00Z")
    assert item.memory.valid_from != "2026-03-14T10:00:00Z"
    result = MemoryEvidenceSource(service, "project").annotate([item])[0]
    assert stamps(result)[0]["statement_at"] == "2026-03-14T10:00:00Z"
    assert stamps(result)[0]["source_message_id"] == "opaque:turn-42"


def test_annotation_keeps_exact_supplied_clip_and_acquisition_fields(service):
    item = admitted(service)
    clip = replace(item, memory=replace(item.memory, content="Cedar"))
    result = MemoryEvidenceSource(service, "project").annotate([clip])[0]
    assert (
        result.memory.content == "Cedar"
        and result.memory.content != item.memory.content
    )
    assert result.score == clip.score and result.channels == clip.channels
    assert result.source_refs == clip.source_refs and result is not clip
    assert clip.memory.structured_content == item.memory.structured_content


def test_metadata_reader_never_reads_event_text(service, monkeypatch):
    item = admitted(service)
    original = service.get_event

    class MetadataOnly(dict):
        def get(self, key, default=None):
            assert key == "source_type", "event text is not metadata"
            return super().get(key, default)

    def metadata(ref):
        event = original(ref)
        return replace(event, payload=MetadataOnly(event.payload))

    monkeypatch.setattr(service, "get_event", metadata)
    assert MemoryEvidenceSource(service, "project").annotate([item])


def test_mixed_sources_keep_each_origin_instead_of_claiming_a_single_speaker(service):
    item = extra_source(service, admitted(service))
    result = MemoryEvidenceSource(service, "project").annotate([item])[0]
    assert {s["role"] for s in stamps(result)} == {"user", "assistant"}
    assert build_evidence_views([result], question="Who said it?")[0].role == "mixed"


@pytest.mark.parametrize(
    "unavailable", ["missing", "foreign", "future", "forgotten", "bad_clock"]
)
def test_unavailable_source_keeps_unknown_ref_and_hides_its_metadata(
    service, monkeypatch, unavailable
):
    item = admitted(service)
    item = extra_source(
        service,
        item,
        namespace="elsewhere" if unavailable == "foreign" else "project",
        when="2026-04-20T10:00:00Z"
        if unavailable == "future"
        else "2026-03-14T11:00:00Z",
    )
    original_event = service.get_event
    original_deleted = service.store.is_tombstoned

    def event(ref):
        value = original_event(ref)
        if ref != "extra":
            return value
        if unavailable == "missing":
            return None
        if unavailable == "bad_clock":
            return replace(value, observed_at="not-a-clock")
        return value

    monkeypatch.setattr(service, "get_event", event)
    monkeypatch.setattr(
        service.store,
        "is_tombstoned",
        lambda kind, ref: (
            unavailable == "forgotten"
            and kind == "event"
            and ref == "extra"
            or original_deleted(kind, ref)
        ),
    )
    result = MemoryEvidenceSource(service, "project", as_of="2026-03-15").annotate(
        [item]
    )[0]
    invalid = next(s for s in stamps(result) if s["source_ref"] == "extra")
    assert invalid == {"source_ref": "extra", "role": "unknown", "statement_at": None}
    view = build_evidence_views([result], question="What is known?")[0]
    assert view.role == "mixed"
    rendered = render_evidence_block([view])
    assert "2026-04-20" not in rendered and "opaque-extra" not in rendered


@pytest.mark.parametrize(
    "mutation",
    ["invented_ref", "partial_refs", "foreign_memory", "invented_clip", "old_version"],
)
def test_caller_cannot_forge_provenance_or_change_material_scope(service, mutation):
    item = extra_source(service, admitted(service))
    if mutation == "invented_ref":
        item = replace(item, source_refs=["invented"])
    elif mutation == "partial_refs":
        item = replace(item, source_refs=item.source_refs[:1])
    elif mutation == "foreign_memory":
        item = replace(item, memory=replace(item.memory, namespace="elsewhere"))
    elif mutation == "invented_clip":
        item = replace(
            item, memory=replace(item.memory, content="The secret budget is 999.")
        )
    else:
        item = replace(
            item, memory=replace(item.memory, version=item.memory.version + 1)
        )
    with pytest.raises(ValueError):
        MemoryEvidenceSource(service, "project").annotate([item])


@pytest.mark.parametrize("hard", [False, True])
def test_forgotten_memory_cannot_be_annotated_from_an_old_snapshot(service, hard):
    item = admitted(service)
    service.forget(item.memory.id, hard=hard)
    with pytest.raises(ValueError):
        MemoryEvidenceSource(service, "project").annotate([item])


def test_annotation_obeys_memory_cutoff_namespace_and_disabled_scope(service):
    item = admitted(service)
    with pytest.raises(ValueError):
        MemoryEvidenceSource(service, "project", as_of="2026-03-01").annotate([item])
    with pytest.raises(ValueError):
        MemoryEvidenceSource(service, "elsewhere").annotate([item])
    service.set_memory_enabled("project", False)
    with pytest.raises(ValueError, match="disabled"):
        MemoryEvidenceSource(service, "project").annotate([item])


@pytest.mark.parametrize("status", [MemoryStatus.CANDIDATE, MemoryStatus.REJECTED])
def test_unadmitted_memory_status_is_not_an_annotation_path(service, status):
    item = admitted(service)
    service.store.db.execute(
        "UPDATE memories SET status=? WHERE id=?", (status.value, item.memory.id)
    )
    with pytest.raises(ValueError):
        MemoryEvidenceSource(service, "project").annotate([item])


def test_previous_reader_annotations_are_replaced_not_trusted(service):
    item = admitted(service, role=None)
    forged = {
        **item.memory.structured_content,
        "role": "assistant",
        "_reader_sources": [{"role": "assistant", "statement_at": "2099-01-01"}],
        "_reader_statement_sources": [
            {
                "source_ref": item.source_refs[0],
                "role": "assistant",
                "statement_at": "2099-01-01",
            }
        ],
    }
    item = replace(item, memory=replace(item.memory, structured_content=forged))
    result = MemoryEvidenceSource(service, "project").annotate([item])[0]
    assert stamps(result)[0]["role"] == "unknown"
    assert "_reader_sources" not in result.memory.structured_content
    assert "2099" not in render_evidence_block(
        build_evidence_views([result], question="Cedar")
    )


def test_custom_metadata_provider_is_injected_without_bypassing_scope(service):
    item = admitted(service)

    class Provider:
        def for_memory(self, memory, source_refs, *, namespace, as_of=None):
            return tuple(SourceStamp(ref, "unknown") for ref in source_refs)

    result = MemoryEvidenceSource(service, "project", provenance=Provider()).annotate(
        [item]
    )[0]
    assert stamps(result)[0]["role"] == "unknown"
    with pytest.raises(ValueError):
        MemoryEvidenceSource(service, "elsewhere", provenance=Provider()).annotate(
            [item]
        )


def test_provider_itself_rejects_incomplete_sources_and_foreign_memories(service):
    item = extra_source(service, admitted(service))
    provider = MemoryServiceProvenanceProvider(service)
    with pytest.raises(ValueError, match="provenance"):
        provider.for_memory(item.memory, item.source_refs[:1], namespace="project")
    with pytest.raises(ValueError, match="project"):
        provider.for_memory(item.memory, item.source_refs, namespace="elsewhere")


@pytest.mark.parametrize(
    "changes",
    [
        {"source_ref": ""},
        {"role": []},
        {"role": "system"},
        {"statement_at": 23},
        {"statement_at": "bad-clock"},
        {"statement_at": ""},
        {"statement_at": " "},
        {"source_message_id": []},
    ],
)
def test_stamp_rejects_malformed_public_metadata(changes):
    with pytest.raises(ValueError):
        SourceStamp(**{"source_ref": "event", **changes})


def test_custom_future_stamp_is_masked_before_rendering(service):
    item = admitted(service)

    class Provider:
        def for_memory(self, memory, source_refs, *, namespace, as_of=None):
            return tuple(
                SourceStamp(ref, "assistant", "2099-01-01T00:00:00Z", "future-message")
                for ref in source_refs
            )

    result = MemoryEvidenceSource(
        service, "project", as_of="2026-03-15", provenance=Provider()
    ).annotate([item])[0]
    assert stamps(result) == [
        {"source_ref": item.source_refs[0], "role": "unknown", "statement_at": None}
    ]


def test_malformed_source_type_remains_unknown_without_reading_text(
    service, monkeypatch
):
    item = admitted(service)
    original = service.get_event
    monkeypatch.setattr(
        service,
        "get_event",
        lambda ref: replace(
            original(ref), payload={"source_type": {}, "text": "claim role=user"}
        ),
    )
    result = MemoryEvidenceSource(service, "project").annotate([item])[0]
    assert stamps(result)[0]["role"] == "unknown"
