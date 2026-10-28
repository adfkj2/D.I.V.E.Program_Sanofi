"""Independent boundaries across consent, source admission and visible extent."""

from dataclasses import replace

import pytest

from dive_memory.dialogue_retention import DialogueRetentionPolicy
from dive_memory.gate import GateAction, GateDecision
from dive_memory.llm import HeuristicExtractionProvider
from dive_memory.models import RetrievalItem
from dive_memory.reading.evidence import build_evidence_views, render_evidence_block
from dive_memory.retrieval_session import MemoryEvidenceSource
from dive_memory.service import MemoryService


def skip(text, **kwargs):
    return GateDecision(False, 0, 0, 0, "ephemeral", "fixture", GateAction.SKIP)


@pytest.mark.parametrize("deferred", [False, True])
def test_disabled_turn_remains_excluded_after_reenable_restart_and_full_rebuild(
    tmp_path, deferred
):
    path = str(tmp_path / "consent.sqlite")
    service = MemoryService(path, gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        if not deferred:
            service.set_memory_enabled("project", False)
        result = service.ingest(
            "project",
            "The copper observatory houses a violet sundial.",
            source_type="user",
            defer=deferred,
        )
        if deferred:
            service.set_memory_enabled("project", False)
            assert service.process_pending()[0]["memory_disabled"]
        else:
            assert result["memory_disabled"]
        assert not service.list_memories("project")
        service.set_memory_enabled("project", True)
    finally:
        service.store.close()

    reopened = MemoryService(path, gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        reopened.rebuild_projections("project")
        assert not reopened.list_memories("project")
        assert not reopened.retrieve("project", "violet sundial").items
        # Event audit retention is distinct from permission to project it.
        assert reopened.get_event(result["event_id"]) is not None
    finally:
        reopened.store.close()


def test_extent_distinguishes_stored_retention_from_later_visible_clip():
    service = MemoryService(
        gate=skip, episodic_policy=DialogueRetentionPolicy(max_chars=60)
    )
    try:
        result = service.ingest(
            "project",
            "  " + "The amber telescope has a cedar case. " * 5,
            source_type="user",
        )
        memory = service.get_memory(result["memory_ids"][0])
        clip = replace(memory, content=memory.content[20:35])
        item = RetrievalItem(clip, 1.0, ["fixture"], memory.source_event_ids)
        annotated = MemoryEvidenceSource(service, "project").annotate([item])[0]
        view = build_evidence_views([annotated], question="What case?")[0]
        assert view.retention_extent == {
            "retained_source_span": [2, 62],
            "original_source_chars": 2
            + len("The amber telescope has a cedar case. " * 5),
            "retained_chars": 60,
            "truncated": True,
            "displayed_chars": 15,
        }
        rendered = render_evidence_block([view])
        assert "retained_source_span=2:62" in rendered
        assert "displayed_chars=15" in rendered
        assert rendered.endswith("\n    " + clip.content)
    finally:
        service.store.close()


@pytest.mark.parametrize("hard", [False, True])
def test_previously_discovered_dialogue_cannot_be_read_or_reannotated_after_forget(
    hard,
):
    service = MemoryService(gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        result = service.ingest(
            "project", "A maroon astrolabe arrived.", source_type="user"
        )
        memory = service.get_memory(result["memory_ids"][0])
        item = RetrievalItem(memory, 1.0, ["fixture"], memory.source_event_ids)
        source = MemoryEvidenceSource(service, "project", initial=[item])
        assert source.annotate([item])
        service.forget(memory.id, hard=hard)
        with pytest.raises(ValueError):
            source.read(memory.id)
        with pytest.raises(ValueError):
            source.annotate([item])
        service.rebuild_projections("project")
        assert not source.search("maroon astrolabe")
    finally:
        service.store.close()


class SegmentGate:
    segment_assertions = True
    supports_source_type = True

    def __init__(self, *, exclude_work=False):
        self.exclude_work = exclude_work

    def __call__(self, text, **kwargs):
        accepted = "?" not in text and not (self.exclude_work and "I work at" in text)
        return GateDecision(
            accepted,
            0.7,
            0.8,
            0.6,
            "medium_term",
            "fixture",
            GateAction.WRITE if accepted else GateAction.SKIP,
            "OK" if accepted else "NON_ASSERTIVE_SPEECH_ACT",
        )


@pytest.mark.parametrize("hard", [False, True])
def test_forgotten_semantic_fact_does_not_return_when_candidate_index_changes(hard):
    service = MemoryService(
        gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I work at Northfield. I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        first, forgotten, observation = result["memory_ids"]
        assert service.get_memory(forgotten).content == "I own a jade compass."
        service.forget(forgotten, hard=hard)
        assert service.get_memory(first).status.value == "ACTIVE"
        assert not service.retrieve("project", "jade compass").items
        service.extractor = HeuristicExtractionProvider(
            gate=SegmentGate(exclude_work=True),
            episodic_policy=DialogueRetentionPolicy(),
        )
        service.rebuild_projections("project")
        assert not any(
            "jade compass" in memory.content
            for memory in service.list_memories("project")
        )
        assert not service.retrieve("project", "jade compass").items
    finally:
        service.store.close()


@pytest.mark.parametrize(
    "role", ["human", "user_correction", "memory_correction", "USER", " user "]
)
def test_supported_user_role_aliases_cannot_bypass_explicit_memory_optout(role):
    service = MemoryService(episodic_policy=DialogueRetentionPolicy())
    try:
        result = service.ingest(
            "project", "Do not remember this: I live in Cedar.", source_type=role
        )
        assert result["memory_ids"] == []
        assert not service.list_memories("project")
    finally:
        service.store.close()


class ReorderedFacts(HeuristicExtractionProvider):
    def extract_outcome(self, text, **kwargs):
        outcome = super().extract_outcome(text, **kwargs)
        facts = [c for c in outcome.candidates if c.evidence_state == "FACT"]
        observations = [c for c in outcome.candidates if c.evidence_state != "FACT"]
        return replace(outcome, candidates=tuple([*reversed(facts), *observations]))


@pytest.mark.parametrize("hard", [False, True])
def test_reordered_fact_projection_preserves_unforgotten_independent_fact(
    tmp_path, hard
):
    path = str(tmp_path / "reordered.sqlite")
    service = MemoryService(
        path, gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I work at Northfield. I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        first, forgotten, _observation = result["memory_ids"]
        assert service.get_memory(first).content == "I work at Northfield."
        service.forget(forgotten, hard=hard)
    finally:
        service.store.close()

    reopened = MemoryService(
        path,
        extractor=ReorderedFacts(
            gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
        ),
    )
    try:
        reopened.rebuild_projections("project")
        contents = [memory.content for memory in reopened.list_memories("project")]
        assert not any("jade compass" in content for content in contents)
        assert "I work at Northfield." in contents
    finally:
        reopened.store.close()


@pytest.mark.parametrize(
    ("source_type", "expected_role"),
    [
        ("USER", "user"),
        (" user ", "user"),
        ("human", "user"),
        ("user_correction", "user"),
        ("memory_correction", "user"),
        (" ASSISTANT ", "assistant"),
    ],
)
def test_admitted_role_metadata_uses_the_same_normalized_source_identity(
    source_type, expected_role
):
    service = MemoryService()
    try:
        result = service.ingest(
            "project",
            "I work at Cedar.",
            explicit=True,
            source_type=source_type,
            observed_at="2026-03-14T10:00:00Z",
        )
        memory = service.get_memory(result["memory_ids"][0])
        item = RetrievalItem(memory, 1.0, ["fixture"], memory.source_event_ids)
        annotated = MemoryEvidenceSource(service, "project").annotate([item])[0]
        stamps = annotated.memory.structured_content["_reader_statement_sources"]
        assert stamps[0]["role"] == expected_role
        assert (
            build_evidence_views([annotated], question="Who said this?")[0].role
            == expected_role
        )
    finally:
        service.store.close()


@pytest.mark.parametrize(
    "text",
    [
        "Do not remember this: I live in Cedar.",
        "I live in Cedar. password: synthetic-placeholder",
    ],
    ids=["explicit-optout", "synthetic-sensitive-label"],
)
def test_consent_exclusion_survives_replaced_extractor_restart_and_rebuild(
    tmp_path, text
):
    path = str(tmp_path / "excluded.sqlite")
    service = MemoryService(path, episodic_policy=DialogueRetentionPolicy())
    try:
        result = service.ingest("project", text, source_type="user")
        assert result["memory_ids"] == []
    finally:
        service.store.close()

    reopened = MemoryService(
        path, extractor=HeuristicExtractionProvider(gate=SegmentGate())
    )
    try:
        reopened.rebuild_projections("project")
        assert not reopened.list_memories("project")
        assert reopened.get_event(result["event_id"]) is not None
    finally:
        reopened.store.close()
