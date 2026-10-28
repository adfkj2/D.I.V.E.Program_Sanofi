"""Verbatim admitted user observations stay distinct from promoted facts."""

import pytest

from dive_memory.dialogue_retention import PREDICATE, VERSION, DialogueRetentionPolicy
from dive_memory.extraction import Candidate
from dive_memory.gate import GateAction, GateDecision
from dive_memory.llm import HeuristicExtractionProvider
from dive_memory.models import EvidenceState
from dive_memory.narrative_retention import NarrativeRetentionPolicy
from dive_memory.service import MemoryService


def skip(text, **kwargs):
    return GateDecision(
        False, 0, 0, 0, "ephemeral", "synthetic low utility", GateAction.SKIP
    )


def candidate(content, *, state="OBSERVATION", role="user"):
    return Candidate(
        content,
        "episode",
        state,
        {"role": role},
        GateDecision(True, 0.5, 0.8, 0.5, "medium_term", "fixture"),
    )


def test_short_user_turn_is_preserved_without_a_verb_or_number_whitelist():
    text = "I realized the object is worth triple the amount I paid."
    result = DialogueRetentionPolicy().candidates(
        text, source_type="user", observed_at="2026-02-02T12:00:00Z", existing=[]
    )
    assert len(result) == 1 and result[0].content == text
    assert result[0].kind == "episode" and result[0].evidence_state == "OBSERVATION"
    assert result[0].structured_content["predicate"] == PREDICATE
    assert result[0].structured_content["retention_policy"] == VERSION
    assert "value" not in result[0].structured_content


def test_full_user_observation_is_not_duplicated_and_partial_observation_is_not_full():
    text = "The crate was delivered. Its contents included two spare seals."
    policy = DialogueRetentionPolicy()
    assert not policy.candidates(
        text, source_type="user", observed_at=None, existing=[candidate(text)]
    )
    assert (
        len(
            policy.candidates(
                text,
                source_type="user",
                observed_at=None,
                existing=[candidate("The crate was delivered.")],
            )
        )
        == 1
    )
    assert (
        len(
            policy.candidates(
                text,
                source_type="user",
                observed_at=None,
                existing=[candidate(text, role="assistant")],
            )
        )
        == 1
    )


def test_truncation_is_exact_bounded_and_disclosed_without_an_invented_body_marker():
    text = "  " + "Original source words. " * 10 + "Tail excluded.  "
    value = DialogueRetentionPolicy(max_chars=50).candidates(
        text, source_type="user", observed_at=None, existing=[]
    )[0]
    start, end = value.structured_content["source_span"]
    assert value.content == text[start:end] and len(value.content) == 50
    assert value.structured_content["truncated"] is True
    assert value.structured_content["source_chars"] == len(text)
    assert value.structured_content["retained_chars"] == 50
    assert "Tail excluded" not in value.content


@pytest.mark.parametrize("role", [None, "assistant", "web", "tool", "summary"])
def test_non_user_sources_are_not_retained(role):
    assert not DialogueRetentionPolicy().candidates(
        "The room has three windows.", source_type=role, observed_at=None, existing=[]
    )


@pytest.mark.parametrize(
    "text",
    [
        "Do not remember this: the crate contains two seals.",
        "Please don't store this conversation: the event is tomorrow.",
        "这次内容不要记住：箱内有两枚备用密封圈。",
        "别保存这段对话，我正在说明一个情况。",
        "My api key is a confidential value. The crate arrived.",
        "The crate arrived. " + "ordinary note " * 200 + " password: private",
    ],
)
def test_full_turn_sensitivity_and_optout_are_checked_before_clipping(text):
    assert not DialogueRetentionPolicy(max_chars=30).candidates(
        text, source_type="user", observed_at=None, existing=[]
    )


@pytest.mark.parametrize(
    "limits",
    [
        {"max_chars": 0},
        {"max_chars": True},
        {"max_chars": 1.2},
        {"max_turn_chars": 0},
        {"max_turn_chars": 10, "max_chars": 20},
    ],
)
def test_invalid_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        DialogueRetentionPolicy(**limits)


def test_limit_does_not_read_an_archive_or_retain_unbounded_input():
    policy = DialogueRetentionPolicy(max_chars=10, max_turn_chars=20)
    assert not policy.candidates(
        "x" * 21, source_type="user", observed_at=None, existing=[]
    )


def test_default_service_stays_unchanged_and_optin_uses_the_same_store_and_indexes():
    text = "I realized the object is worth triple the amount I paid."
    ordinary = MemoryService(gate=skip)
    opted = MemoryService(gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        assert ordinary.ingest("project", text, source_type="user")["memory_ids"] == []
        event = opted.ingest(
            "project", text, source_type="user", observed_at="2026-02-02T12:00:00Z"
        )
        memory = opted.get_memory(event["memory_ids"][0])
        assert (
            memory.content == text
            and memory.evidence_state is EvidenceState.OBSERVATION
        )
        assert memory.source_event_ids == [event["event_id"]]
        assert opted.retrieve("project", "object worth amount").items
        assert not opted.retrieve("other", "object worth amount").items
        assert not opted.profile("project").get("residence")
    finally:
        ordinary.store.db.close()
        opted.store.db.close()


@pytest.mark.parametrize("deferred", [False, True])
def test_memory_disabled_turns_cannot_be_projected_later(deferred):
    service = MemoryService(gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        if not deferred:
            service.set_memory_enabled("project", False)
        result = service.ingest(
            "project",
            "A measured response was noted.",
            source_type="user",
            defer=deferred,
        )
        if deferred:
            service.set_memory_enabled("project", False)
            assert service.process_pending()[0]["memory_disabled"]
        else:
            assert result["memory_disabled"]
        service.set_memory_enabled("project", True)
        assert not service.process_pending() and not service.list_memories("project")
    finally:
        service.store.db.close()


def test_optout_is_enforced_before_an_ordinary_gate_can_accept_a_fact():
    service = MemoryService(episodic_policy=DialogueRetentionPolicy())
    try:
        result = service.ingest("project", "不要记住：我住在北洲。", source_type="user")
        assert result["memory_ids"] == []
        assert not service.list_memories("project")
    finally:
        service.store.db.close()


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


@pytest.mark.parametrize("hard", [False, True])
def test_forgetting_a_fact_blocks_same_source_full_turn_and_rebuild_under_changed_extraction(
    hard,
):
    service = MemoryService(
        gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I work at Northfield. I own a jade compass. How should I catalogue it?",
            source_type="user",
            idempotency_key="episode-forgetting",
        )
        memories = [service.get_memory(memory_id) for memory_id in result["memory_ids"]]
        assert len(memories) == 3
        first, forgotten, observation = memories
        assert first.content == "I work at Northfield."
        assert forgotten.content == "I own a jade compass."
        assert observation.structured_content["predicate"] == PREDICATE
        service.forget(forgotten.id, hard=hard)
        assert service.get_memory(first.id).status.value != "DELETED"
        assert not any(
            "jade compass" in hit.memory.content
            for hit in service.retrieve("project", "jade compass", intent="all").items
        )
        # Changed candidate ordering makes memory-ID tombstones alone insufficient:
        # the whole-turn observation would now take the first (not-forgotten) ID.
        service.extractor = HeuristicExtractionProvider(
            gate=skip, episodic_policy=DialogueRetentionPolicy()
        )
        service.rebuild_projections("project")
        assert not any(
            "jade compass" in memory.content
            for memory in service.list_memories("project")
        )
    finally:
        service.store.db.close()


def test_same_text_at_distinct_source_events_keeps_distinct_episode_identity():
    service = MemoryService(gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        first = service.ingest(
            "project",
            "The crate contains two spare seals.",
            source_type="user",
            observed_at="2026-02-02T12:00:00Z",
            idempotency_key="first",
        )
        second = service.ingest(
            "project",
            "The crate contains two spare seals.",
            source_type="user",
            observed_at="2026-02-03T12:00:00Z",
            idempotency_key="second",
        )
        assert set(first["memory_ids"]).isdisjoint(second["memory_ids"])
        assert service.get_memory(first["memory_ids"][0]).source_event_ids == [
            first["event_id"]
        ]
        assert service.get_memory(second["memory_ids"][0]).source_event_ids == [
            second["event_id"]
        ]
    finally:
        service.store.db.close()


@pytest.mark.parametrize("hard", [False, True])
def test_direct_store_forgetting_is_atomic_with_dialogue_eviction(hard, monkeypatch):
    service = MemoryService(
        gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        fact_id, observation_id = result["memory_ids"]
        original = service.store.block_dialogue_source

        def fail_after_block(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected source-block failure")

        monkeypatch.setattr(service.store, "block_dialogue_source", fail_after_block)
        forget = service.store.purge_memory if hard else service.store.tombstone_memory
        with pytest.raises(RuntimeError, match="injected source-block failure"):
            forget(fact_id, "user_request", "2026-02-04T12:00:00Z")
        for memory_id in (fact_id, observation_id):
            assert service.get_memory(memory_id).status.value == "ACTIVE"
            assert not service.store.is_tombstoned("memory", memory_id)
        assert not service.store.dialogue_source_blocked("project", result["event_id"])
        assert any(
            hit.memory.id == observation_id
            for hit in service.retrieve("project", "jade compass").items
        )

        monkeypatch.setattr(service.store, "block_dialogue_source", original)
        forget(fact_id, "user_request", "2026-02-04T12:00:00Z")
        assert service.store.dialogue_source_blocked("project", result["event_id"])
        observation = service.get_memory(observation_id)
        assert observation is None if hard else observation.status.value == "DELETED"
        assert not service.retrieve("project", "jade compass").items
    finally:
        service.store.db.close()


def test_dialogue_source_guard_is_durable_and_scoped_to_source_and_namespace(tmp_path):
    path = str(tmp_path / "dialogue.sqlite")
    service = MemoryService(
        path, gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    result = service.ingest(
        "project",
        "I own a jade compass. How should I catalogue it?",
        source_type="user",
    )
    independent = service.ingest(
        "project", "A blue vase was noted.", source_type="user"
    )
    other = service.ingest("other", "A jade compass was noted.", source_type="user")
    service.forget(result["memory_ids"][0], hard=True)
    service.store.db.close()

    reopened = MemoryService(path, gate=skip, episodic_policy=DialogueRetentionPolicy())
    try:
        assert reopened.store.dialogue_source_blocked("project", result["event_id"])
        assert not reopened.store.dialogue_source_blocked("other", result["event_id"])
        assert not reopened.store.dialogue_source_blocked(
            "project", independent["event_id"]
        )
        reopened.rebuild_projections()
        assert not any(
            "jade compass" in memory.content
            for memory in reopened.list_memories("project")
        )
        assert reopened.list_memories("other")[0].content == "A jade compass was noted."
        assert reopened.store.memories_for_event(independent["event_id"])
        assert reopened.store.memories_for_event(other["event_id"])
        decisions = reopened.store.db.execute(
            "SELECT accepted,outcome_code FROM write_decisions WHERE event_id=?",
            (result["event_id"],),
        ).fetchall()
        assert decisions and all(
            not row["accepted"] and row["outcome_code"] == "POLICY_SKIP"
            for row in decisions
        )
    finally:
        reopened.store.db.close()


@pytest.mark.parametrize("hard", [False, True])
def test_event_forgetting_handles_multiple_memories_without_resurrecting_dialogue(hard):
    service = MemoryService(
        gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        result = service.ingest(
            "project",
            "I work at Northfield. I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        assert len(result["memory_ids"]) == 3
        assert set(service.forget_event(result["event_id"], hard=hard)) == set(
            result["memory_ids"]
        )
        service.rebuild_projections("project")
        assert not service.list_memories("project")
        assert service.store.is_tombstoned("event", result["event_id"])
    finally:
        service.store.db.close()


def test_forgetting_merged_semantic_fact_blocks_each_recorded_source_observation():
    service = MemoryService(
        gate=SegmentGate(), episodic_policy=DialogueRetentionPolicy()
    )
    try:
        first = service.ingest(
            "project",
            "I own a jade compass. How should I catalogue it?",
            source_type="user",
        )
        second = service.ingest(
            "project",
            "I own a jade compass. How should I carry it?",
            source_type="user",
        )
        assert first["memory_ids"][0] == second["memory_ids"][0]
        assert first["memory_ids"][1] != second["memory_ids"][1]
        service.forget(first["memory_ids"][0], hard=True)
        assert all(
            service.store.dialogue_source_blocked("project", result["event_id"])
            for result in (first, second)
        )
        assert not service.retrieve("project", "jade compass").items
    finally:
        service.store.db.close()


def test_sensitive_turn_is_excluded_before_base_policy_and_fact_gate():
    class NeverCalled:
        def __call__(self, *args, **kwargs):
            pytest.fail("blocked content reached semantic extraction")

        def candidates(self, *args, **kwargs):
            pytest.fail("blocked content reached the composed episode policy")

    provider = HeuristicExtractionProvider(
        gate=NeverCalled(), episodic_policy=DialogueRetentionPolicy(base=NeverCalled())
    )
    outcome = provider.extract_outcome(
        "I live in Northfield. password: confidential", source_type="user"
    )
    assert not outcome.candidates and outcome.code.value == "POLICY_SKIP"


@pytest.mark.parametrize("hard", [False, True])
def test_composed_complete_narrative_is_deduplicated_but_cannot_bypass_forgetting(hard):
    gate = SegmentGate()
    policy = DialogueRetentionPolicy(base=NarrativeRetentionPolicy(gate))
    service = MemoryService(gate=gate, episodic_policy=policy)
    text = "I work at Northfield. I own a jade compass. How should I catalogue it?"
    try:
        result = service.ingest("project", text, source_type="user")
        assert len(result["memory_ids"]) == 3
        first, forgotten, observation = result["memory_ids"]
        assert service.get_memory(observation).content == text
        service.forget(forgotten, hard=hard)
        assert service.get_memory(first).status.value == "ACTIVE"
        assert not any(
            "jade compass" in item.memory.content
            for item in service.retrieve("project", "jade compass").items
        )
        service.extractor = HeuristicExtractionProvider(
            gate=skip,
            episodic_policy=DialogueRetentionPolicy(
                base=NarrativeRetentionPolicy(skip)
            ),
        )
        service.rebuild_projections("project")
        assert not any(
            "jade compass" in memory.content
            for memory in service.list_memories("project")
        )
    finally:
        service.store.db.close()


def test_protection_does_not_mutate_source_candidates_or_promote_semantic_facts():
    observation = candidate("I saw a violet vase.")
    fact = candidate("I own a jade compass.", state="FACT")
    assistant = candidate("I saw a violet vase.", role="assistant")
    guarded, retained_fact, retained_assistant = (
        DialogueRetentionPolicy.protect_candidates(
            [observation, fact, assistant], source_type="user"
        )
    )
    assert guarded is not observation and guarded.content == observation.content
    assert "dialogue_source_guard" not in observation.structured_content
    assert guarded.structured_content["dialogue_source_guard"] == VERSION
    assert retained_fact is fact and retained_assistant is assistant
