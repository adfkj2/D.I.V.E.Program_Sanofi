from dataclasses import replace

import pytest

from dive_memory.models import EvidenceState, Memory
from dive_memory.resolution import (
    MemoryRelationship,
    RelationshipResolver,
    ResolutionAction,
    ResolutionContext,
)
from dive_memory.service import MemoryService


def _memory(memory_id="a", predicate="residence", value="Chengdu", **kwargs):
    return Memory(
        memory_id, "u1", "semantic_fact", EvidenceState.FACT,
        f"{predicate}: {value}", {"subject": "user", "predicate": predicate, "value": value},
        confidence=kwargs.pop("confidence", 0.8),
        valid_from=kwargs.pop("valid_from", "2025-01-01T00:00:00+00:00"),
        source_event_ids=kwargs.pop("source_event_ids", [f"evt-{memory_id}"]),
        **kwargs,
    )


@pytest.mark.parametrize(("candidate", "context", "expected"), [
    (_memory("b", predicate="preference", value="tea"), ResolutionContext("message", "I like tea"), MemoryRelationship.UNRELATED),
    (_memory("b"), ResolutionContext("message", "I live in Chengdu"), MemoryRelationship.DUPLICATE),
    (_memory("b"), ResolutionContext("message", "I confirm again that I live in Chengdu"), MemoryRelationship.REINFORCEMENT),
    (_memory("b", predicate="project", value="healthcare AI memory system"),
     ResolutionContext("message", "more specifically, a healthcare AI memory system"), MemoryRelationship.REFINEMENT),
    (_memory("b", value="Shanghai"), ResolutionContext("memory_correction", "Correction: Shanghai", forced=True), MemoryRelationship.CORRECTION),
    (_memory("b", value="Shanghai", valid_from="2026-01-01T00:00:00+00:00"),
     ResolutionContext("message", "I moved to Shanghai"), MemoryRelationship.TEMPORAL_UPDATE),
    (replace(_memory("b", value="Shanghai"), confidence=0.4),
     ResolutionContext("message", "No, I do not live in Chengdu"), MemoryRelationship.CONTRADICTION),
    (_memory("b", predicate="primary_tool", value="VS Code", valid_from=None),
     ResolutionContext("message", "I now use VS Code"), MemoryRelationship.SUPERSESSION),
])
def test_relationship_classifier_covers_the_full_matrix(candidate, context, expected):
    existing = _memory(predicate="project", value="AI system") if expected is MemoryRelationship.REFINEMENT else _memory()
    if expected is MemoryRelationship.SUPERSESSION:
        existing = _memory(predicate="primary_tool", value="Jupyter", valid_from=None)
    proposal = RelationshipResolver().classify(existing, candidate, context)
    assert proposal.relationship is expected
    assert proposal.resolver_version == "relationship-rules-v1"


def test_duplicate_merges_provenance_without_creating_another_memory():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    second = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)

    assert second["memory_ids"] == first["memory_ids"]
    memory = service.get_memory(first["memory_ids"][0])
    assert set(memory.source_event_ids) == {first["event_id"], second["event_id"]}
    assert len(service.list_memories("u1")) == 1
    transition = service.store.transitions_for_memory(memory.id)[-1]
    assert transition["relationship"] == "duplicate"
    assert transition["action"] == "MERGE_PROVENANCE"


def test_temporal_update_records_immutable_transition_and_preserves_history():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    second = service.ingest("u1", "我住在上海", explicit=True, observed_at="2026-01-01T00:00:00+00:00")

    old = service.get_memory(first["memory_ids"][0])
    new = service.get_memory(second["memory_ids"][0])
    assert old.valid_to == "2026-01-01T00:00:00+00:00"
    assert new.supersedes_id == old.id
    transitions = service.store.transitions_for_memory(new.id)
    assert transitions[-1]["relationship"] == "temporal_update"
    assert transitions[-1]["from_memory_id"] == old.id
    assert transitions[-1]["to_memory_id"] == new.id
    assert service.timeline("u1", as_of="2025-06-01") == [old]


def test_explicit_correction_is_audited_as_correction():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    corrected = service.correct_memory(first["memory_ids"][0], "我喜欢咖啡")

    transition = service.store.transitions_for_memory(corrected["memory_ids"][0])[-1]
    assert transition["relationship"] == "correction"
    assert transition["action"] == "SUPERSEDE"


# ---------------------------------------------------------------------------
# Provenance guard: the false-memory suite's ``sd-summary-overwrites-user``
# case. A generated summary is a confident, later-dated, single-valued fact, so
# the content rules alone would supersede the user's own statement. What stops
# it is provenance, which must therefore be part of the resolution context.
# ---------------------------------------------------------------------------
_DERIVED_VS_AUTHORITATIVE = [
    ("sd-summary-overwrites-user", "summary"),
    ("sd-generated-overwrites-user", "generated"),
    ("sd-tool-overwrites-user", "tool"),
    ("sd-assistant-overwrites-user", "assistant"),
]


@pytest.mark.parametrize(("case_id", "derived_source"), _DERIVED_VS_AUTHORITATIVE)
def test_derived_source_cannot_supersede_an_authoritative_fact(case_id, derived_source):
    existing = _memory("a", value="Chengdu")
    candidate = _memory("b", value="Beijing", valid_from="2026-08-02T00:00:00+00:00")
    context = ResolutionContext(
        "message", "我住在北京", source_type=derived_source, existing_source_type="user",
    )
    proposal = RelationshipResolver().classify(existing, candidate, context)

    assert proposal.action is not ResolutionAction.SUPERSEDE, case_id
    assert proposal.action is ResolutionAction.COEXIST
    assert proposal.relationship is MemoryRelationship.CONTRADICTION


def test_derived_restatement_of_the_same_value_merges_provenance():
    existing = _memory("a", value="Chengdu")
    candidate = _memory("b", value="Chengdu", valid_from="2026-08-04T00:00:00+00:00")
    context = ResolutionContext(
        "message", "我住在成都", source_type="summary", existing_source_type="user",
    )
    proposal = RelationshipResolver().classify(existing, candidate, context)

    assert proposal.action is ResolutionAction.MERGE_PROVENANCE
    assert proposal.relationship is MemoryRelationship.REINFORCEMENT


def test_derived_source_may_supersede_another_derived_fact():
    existing = _memory("a", value="Chengdu")
    candidate = _memory("b", value="Beijing", valid_from="2026-08-02T00:00:00+00:00")
    context = ResolutionContext(
        "message", "我住在北京", source_type="summary", existing_source_type="summary",
    )
    proposal = RelationshipResolver().classify(existing, candidate, context)

    assert proposal.action is ResolutionAction.SUPERSEDE


def test_user_can_still_supersede_a_user_fact():
    existing = _memory("a", value="Chengdu")
    candidate = _memory("b", value="Beijing", valid_from="2026-08-02T00:00:00+00:00")
    context = ResolutionContext(
        "message", "我住在北京", source_type="user", existing_source_type="user",
    )
    proposal = RelationshipResolver().classify(existing, candidate, context)

    assert proposal.action is ResolutionAction.SUPERSEDE


def test_unknown_provenance_keeps_the_pre_v2_behaviour():
    """``None`` on either side must not change the rules v1 callers rely on."""
    existing = _memory("a", value="Chengdu")
    candidate = _memory("b", value="Beijing", valid_from="2026-08-02T00:00:00+00:00")
    proposal = RelationshipResolver().classify(
        existing, candidate, ResolutionContext("message", "我住在北京"),
    )

    assert proposal.action is ResolutionAction.SUPERSEDE


def test_service_prevents_a_summary_from_closing_a_user_fact():
    """End-to-end reproduction of ``sd-summary-overwrites-user``."""
    service = MemoryService()
    first = service.ingest(
        "u1", "我住在成都", explicit=True, source_type="user",
        observed_at="2025-01-01T00:00:00+00:00",
    )
    service.ingest(
        "u1", "我住在北京", explicit=True, source_type="summary",
        observed_at="2026-08-02T00:00:00+00:00",
    )

    original = service.get_memory(first["memory_ids"][0])
    assert original.status.value == "ACTIVE"
    assert original.valid_to is None
    assert service.profile("u1")["residence"] == "成都"


def test_service_still_lets_a_user_correct_their_own_residence():
    service = MemoryService()
    first = service.ingest(
        "u1", "我住在成都", explicit=True, source_type="user",
        observed_at="2025-01-01T00:00:00+00:00",
    )
    service.ingest(
        "u1", "我住在北京", explicit=True, source_type="user",
        observed_at="2026-08-02T00:00:00+00:00",
    )

    original = service.get_memory(first["memory_ids"][0])
    assert original.valid_to == "2026-08-02T00:00:00+00:00"
    assert service.profile("u1")["residence"] == "北京"


def test_memory_source_type_reports_unknown_when_events_disagree():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True, source_type="user")
    second = service.ingest("u1", "请记住我喜欢绿茶", explicit=True, source_type="summary")
    memory = service.get_memory(first["memory_ids"][0])

    assert set(memory.source_event_ids) == {first["event_id"], second["event_id"]}
    assert service._memory_source_type(memory) is None


def test_memory_source_type_reads_the_single_recorded_tier():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True, source_type="user")
    memory = service.get_memory(result["memory_ids"][0])

    assert service._memory_source_type(memory) == "user"


def test_hard_purge_removes_transition_references():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    service.ingest("u1", "我住在上海", explicit=True, observed_at="2026-01-01T00:00:00+00:00")
    service.forget(first["memory_ids"][0], hard=True)

    assert service.store.db.execute(
        "SELECT 1 FROM memory_transitions WHERE from_memory_id=? OR to_memory_id=?",
        (first["memory_ids"][0], first["memory_ids"][0]),
    ).fetchone() is None


def test_resolution_uses_indexed_keys_instead_of_loading_all_active_memories(monkeypatch):
    service = MemoryService()
    service.ingest("u1", "请记住项目事实 project-1", explicit=True)

    monkeypatch.setattr(
        service.store, "active_memories",
        lambda namespace: (_ for _ in ()).throw(AssertionError("full active-memory scan")),
    )
    service.ingest("u1", "请记住项目事实 project-2", explicit=True)

    plan = service.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT memory_id FROM memory_keys "
        "WHERE namespace=? AND subject_key=? AND predicate_key=? AND value_key=?",
        ("u1", "user", "statement", "project-2"),
    ).fetchall()
    assert "memory_keys_lookup_idx" in " ".join(str(column) for row in plan for column in row)
