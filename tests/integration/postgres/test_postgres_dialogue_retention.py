"""Native PostgreSQL contract for opt-in same-source forgetting protection."""

import os
from dataclasses import replace

import pytest

from dive_memory.dialogue_retention import DialogueRetentionPolicy
from dive_memory.gate import GateAction, GateDecision
from dive_memory.llm import HeuristicExtractionProvider
from dive_memory.service import MemoryService

pytestmark = pytest.mark.skipif(
    not os.environ.get("DIVE_TEST_POSTGRES_DSN"),
    reason="set DIVE_TEST_POSTGRES_DSN for the opt-in PostgreSQL suite",
)


class SegmentGate:
    segment_assertions = True
    supports_source_type = True

    def __call__(self, text, **kwargs):
        accepted = "?" not in text
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


def skip(text, **kwargs):
    return GateDecision(False, 0, 0, 0, "ephemeral", "fixture", GateAction.SKIP)


@pytest.mark.parametrize("hard", [False, True])
def test_postgres_forgetting_blocks_same_source_dialogue_after_extractor_change(
    isolated_postgres_dsn, hard
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    service = MemoryService(
        store=store, gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I work at Northfield. I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        first, forgotten, observation = result["memory_ids"]
        service.forget(forgotten, hard=hard)
        assert store.dialogue_source_blocked("project", result["event_id"])
        assert not store.dialogue_source_blocked("other", result["event_id"])
        assert service.get_memory(first).status.value == "ACTIVE"
        retained = service.get_memory(observation)
        assert retained is None if hard else retained.status.value == "DELETED"
        assert not any(
            "jade compass" in hit.memory.content
            for hit in service.retrieve("project", "jade compass").items
        )
        # PostgreSQL has no general rebuild API. Reprocessing the original
        # admitted event verifies the same guard before its candidate ID moves.
        service.extractor = HeuristicExtractionProvider(
            gate=skip, episodic_policy=DialogueRetentionPolicy()
        )
        with store.transaction():
            assert service._process_event(store.get_event(result["event_id"])) == []
        decision = store.connection.execute(
            "SELECT accepted,outcome_code FROM write_decisions WHERE event_id=%s AND candidate_index=0",
            (result["event_id"],),
        ).fetchone()
        assert decision == {"accepted": False, "outcome_code": "POLICY_SKIP"}
    finally:
        store.purge_namespace("project")
        store.close()


@pytest.mark.parametrize("hard", [False, True])
def test_postgres_dialogue_eviction_and_source_tombstone_roll_back_together(
    isolated_postgres_dsn, hard, monkeypatch
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    service = MemoryService(
        store=store, gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        original = store.block_dialogue_source

        def fail_after_block(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected dialogue eviction failure")

        monkeypatch.setattr(store, "block_dialogue_source", fail_after_block)
        forget = store.purge_memory if hard else store.tombstone_memory
        with pytest.raises(RuntimeError, match="injected dialogue eviction failure"):
            forget(result["memory_ids"][0], "user_request", "2026-02-04T12:00:00Z")
        assert not store.dialogue_source_blocked("project", result["event_id"])
        for memory_id in result["memory_ids"]:
            assert service.get_memory(memory_id).status.value == "ACTIVE"
            assert not store.is_tombstoned("memory", memory_id)
    finally:
        store.purge_namespace("project")
        store.close()


class ReorderedFacts(HeuristicExtractionProvider):
    def extract_outcome(self, text, **kwargs):
        outcome = super().extract_outcome(text, **kwargs)
        facts = [c for c in outcome.candidates if c.evidence_state == "FACT"]
        observations = [c for c in outcome.candidates if c.evidence_state != "FACT"]
        return replace(outcome, candidates=tuple([*reversed(facts), *observations]))


@pytest.mark.parametrize("hard", [False, True])
def test_postgres_forgotten_fact_is_not_recreated_under_another_candidate_index(
    isolated_postgres_dsn, hard
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    service = MemoryService(
        store=store, gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I work at Northfield. I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        first, forgotten, _observation = result["memory_ids"]
        assert service.get_memory(forgotten).content == "I own a jade compass."
        service.forget(forgotten, hard=hard)
        service.extractor = ReorderedFacts(
            gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
        )
        with store.transaction():
            reprojected = service._process_event(store.get_event(result["event_id"]))
        assert first in reprojected
        assert service.get_memory(first).content == "I work at Northfield."
        assert service.get_memory(first).status.value != "DELETED"
        assert not any(
            "jade compass" in hit.memory.content
            for hit in service.retrieve("project", "jade compass").items
        )
    finally:
        store.purge_namespace("project")
        store.close()


@pytest.mark.parametrize(
    "role", ["human", "user_correction", "memory_correction", "USER", " user "]
)
def test_postgres_user_role_aliases_respect_explicit_optout(
    isolated_postgres_dsn, role
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    service = MemoryService(store=store, episodic_policy=DialogueRetentionPolicy())
    try:
        result = service.ingest(
            "project", "Do not remember this: I live in Cedar.", source_type=role
        )
        assert result["memory_ids"] == []
        assert not service.list_memories("project")
    finally:
        store.purge_namespace("project")
        store.close()


@pytest.mark.parametrize("deferred", [False, True])
def test_postgres_disabled_turn_cannot_be_reprojected_after_reenable(
    isolated_postgres_dsn, deferred
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    service = MemoryService(
        store=store, gate=skip, episodic_policy=DialogueRetentionPolicy()
    )
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
        service.set_memory_enabled("project", True)
        with store.transaction():
            assert service._process_event(store.get_event(result["event_id"])) == []
        assert not service.retrieve("project", "violet sundial").items
        assert service.get_event(result["event_id"]) is not None
    finally:
        store.purge_namespace("project")
        store.close()


@pytest.mark.parametrize(
    "text",
    [
        "Do not remember this: I live in Cedar.",
        "I live in Cedar. password: synthetic-placeholder",
    ],
    ids=["explicit-optout", "synthetic-sensitive-label"],
)
def test_postgres_consent_exclusion_survives_extractor_change(
    isolated_postgres_dsn, text
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    service = MemoryService(store=store, episodic_policy=DialogueRetentionPolicy())
    try:
        result = service.ingest("project", text, source_type="user")
        assert result["memory_ids"] == []
        service.extractor = HeuristicExtractionProvider(gate=SegmentGate())
        with store.transaction():
            assert service._process_event(store.get_event(result["event_id"])) == []
        assert not service.list_memories("project")
        assert service.get_event(result["event_id"]) is not None
    finally:
        store.purge_namespace("project")
        store.close()
