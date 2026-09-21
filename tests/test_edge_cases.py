"""Regression tests for the edge cases found during the code review.

Every test here pins a behaviour that was previously wrong; the analysis and the
before/after evidence live in ``docs/13-code-review-and-fixes.md``.
"""

import pytest
import sqlite3

from dive_memory.ids import new_id
from dive_memory.models import Event, EvidenceState, Memory, MemoryStatus
from dive_memory.planner import plan_query, query_predicates
from dive_memory.service import MemoryService
from dive_memory.store import SQLiteStore
from dive_memory.temporal import parse_instant, visible_at

CHENGDU = "2025-01-01T00:00:00+00:00"
SHANGHAI = "2026-03-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Supersession: only single-valued predicates replace a fact
# --------------------------------------------------------------------------- #

def test_unrelated_statements_do_not_supersede_each_other():
    service = MemoryService()
    first = service.ingest("u1", "请记住我养了一只猫", explicit=True)
    second = service.ingest("u1", "请记住我对花生过敏", explicit=True)

    assert service.get_memory(first["memory_ids"][0]).status.value == "ACTIVE"
    assert service.get_memory(second["memory_ids"][0]).supersedes_id is None
    assert len(service.list_memories("u1", status="ACTIVE")) == 2


def test_two_preferences_coexist_instead_of_burying_each_other():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    second = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)

    assert service.get_memory(first["memory_ids"][0]).status.value == "ACTIVE"
    assert service.get_memory(second["memory_ids"][0]).status.value == "ACTIVE"
    relations = service.store.db.execute(
        "SELECT COUNT(*) FROM relations WHERE namespace='u1'").fetchone()[0]
    assert relations == 2
    assert service.profile("u1")["preference"] == ["绿茶", "咖啡"]


def test_single_valued_predicate_still_supersedes():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at=CHENGDU)
    second = service.ingest("u1", "我住在上海", explicit=True, observed_at=SHANGHAI)

    old = service.get_memory(first["memory_ids"][0])
    assert old.status.value == "SUPERSEDED"
    assert old.valid_to == SHANGHAI
    assert service.get_memory(second["memory_ids"][0]).supersedes_id == old.id


# --------------------------------------------------------------------------- #
# Time semantics
# --------------------------------------------------------------------------- #

def test_parse_instant_normalises_wall_clock_periods():
    assert parse_instant("2025").isoformat() == "2025-12-31T23:59:59.999999+00:00"
    assert parse_instant("2025-06").isoformat() == "2025-06-30T23:59:59.999999+00:00"
    assert parse_instant("2025-06-01").isoformat() == "2025-06-01T23:59:59.999999+00:00"
    assert parse_instant("2025年6月1日").isoformat() == "2025-06-01T23:59:59.999999+00:00"
    assert parse_instant("2025-01-01T00:00:00Z").isoformat() == "2025-01-01T00:00:00+00:00"
    # A naive timestamp is read as UTC rather than raising on comparison.
    assert parse_instant("2025-01-01T00:00:00").isoformat() == "2025-01-01T00:00:00+00:00"
    assert parse_instant("not a date") is None
    assert parse_instant("2025-02-31") is None
    assert parse_instant(None) is None


def test_visible_at_boundaries():
    # A fact that becomes valid on the as-of day is still visible that day.
    assert visible_at(CHENGDU, None, "2025-01-01") is True
    assert visible_at("2026-03-01T00:00:00+00:00", None, "2025-06-01") is False
    # A fact that ends exactly at as_of is no longer valid at as_of.
    assert visible_at(None, "2025-06-01T00:00:00+00:00", "2025-06-01") is False
    assert visible_at(None, "2025-06-02T00:00:00+00:00", "2025-06-01") is True
    # An unparsable bound is "unknown" and must not hide the fact.
    assert visible_at("garbage", None, "2025-06-01") is True
    assert visible_at(None, None, None) is True


def test_year_question_returns_the_fact_valid_during_that_year():
    service = MemoryService()
    chengdu = service.ingest("u1", "我住在成都。", explicit=True, observed_at=CHENGDU)
    shanghai = service.ingest("u1", "我住在上海。", explicit=True, observed_at=SHANGHAI)

    plan = plan_query("2025年我住在哪里")
    assert plan.as_of is not None
    assert parse_instant(plan.as_of).isoformat() == "2025-12-31T23:59:59.999999+00:00"
    assert plan.temporal_mode == "historical"

    historical = [item.memory.id for item in service.retrieve("u1", "2025年我住在哪里").items]
    current = [item.memory.id for item in service.retrieve("u1", "我现在住在哪里").items]
    assert historical == chengdu["memory_ids"]
    assert current == shanghai["memory_ids"]


def test_first_and_latest_queries_apply_temporal_order():
    service = MemoryService()
    chengdu = service.ingest("u1", "我住在成都。", explicit=True, observed_at=CHENGDU)
    shanghai = service.ingest("u1", "我住在上海。", explicit=True, observed_at=SHANGHAI)

    earliest = service.retrieve("u1", "第一次住在哪里", limit=1)
    latest = service.retrieve("u1", "latest residence", limit=1)
    assert earliest.items[0].memory.id == chengdu["memory_ids"][0]
    assert latest.items[0].memory.id == shanghai["memory_ids"][0]


def test_bitemporal_observation_cutoff_and_interval_timeline():
    service = MemoryService()
    result = service.ingest(
        "u1", "我住在成都", explicit=True,
        observed_at="2026-01-01T00:00:00+00:00",
        occurred_from="2025-01-01T00:00:00+00:00",
        occurred_to="2025-07-01T00:00:00+00:00",
    )

    # Retrospectively the fact was valid in 2025, but the system had not
    # observed its evidence at the earlier knowledge-time cutoff.
    assert service.retrieve("u1", "成都", as_of="2025-06-01").items
    assert service.retrieve(
        "u1", "成都", as_of="2025-06-01", observed_as_of="2025-12-31",
    ).items == []
    assert [memory.id for memory in service.timeline(
        "u1", from_time="2025-03-01", to_time="2025-04-01",
    )] == result["memory_ids"]
    assert service.timeline("u1", from_time="2025-08-01", to_time="2025-09-01") == []


def test_invalid_event_interval_is_rejected():
    service = MemoryService()
    with pytest.raises(ValueError, match="earlier"):
        service.ingest("u1", "我住在成都", explicit=True,
                       occurred_from="2025-02-01", occurred_to="2025-01-01")


def test_unparsable_as_of_is_rejected_instead_of_silently_ignored():
    service = MemoryService()
    service.ingest("u1", "我住在上海。", explicit=True)
    with pytest.raises(ValueError):
        service.retrieve("u1", "上海", as_of="何时")
    with pytest.raises(ValueError):
        service.timeline("u1", as_of="???")


# --------------------------------------------------------------------------- #
# Retrieval quality
# --------------------------------------------------------------------------- #

def test_natural_language_questions_reach_structured_facts():
    service = MemoryService()
    service.ingest("u1", "我住在上海。", explicit=True)
    service.ingest("u1", "我喜欢绿茶。", explicit=True)
    service.ingest("u1", "我的目标是学会法语。", explicit=True)

    expectations = {
        "我现在住在哪里": "residence: 上海",
        "我住在哪": "residence: 上海",
        "现在住哪里": "residence: 上海",
        "我喜欢什么": "preference: 绿茶",
        "我的目标是什么": "goal: 学会法语",
    }
    for question, expected in expectations.items():
        items = service.retrieve("u1", question).items
        assert items, f"{question!r} abstained even though the answer is stored"
        assert items[0].memory.content == expected
        assert "predicate" in items[0].channels


def test_explicit_multi_hop_query_traverses_bounded_relation_neighbors():
    from dive_memory.retrieval import RetrievalConfig

    service = MemoryService()
    residence = service.ingest("u1", "我住在成都", explicit=True)
    preference = service.ingest("u1", "我喜欢绿茶", explicit=True)

    result = service.retrieve(
        "u1", "与成都关联的记忆", limit=10,
        config=RetrievalConfig.full(include_multi_hop=True),
    )
    returned = {item.memory.id: item for item in result.items}
    assert set(returned) == {residence["memory_ids"][0], preference["memory_ids"][0]}
    assert "multi_hop" in returned[preference["memory_ids"][0]].channels
    assert result.plan["intent"] == "multi_hop"


def test_unrelated_question_still_abstains():
    service = MemoryService()
    service.ingest("u1", "我住在上海。", explicit=True)
    assert service.retrieve("u1", "火星上有什么").items == []


def test_predicate_lookup_requires_the_question_to_be_about_the_user():
    # The predicate channel must not let an unknown subject inherit the user's
    # own fact — that would be a false memory.
    assert query_predicates("我现在住在哪里") == ["residence"]
    assert query_predicates("我现在住哪里") == ["residence"]
    assert query_predicates("火星住哪里") == []
    assert query_predicates("小王住在哪里") == []

    service = MemoryService()
    service.ingest("u1", "我住在上海。", explicit=True)
    assert service.retrieve("u1", "火星住哪里").items == []
    assert service.retrieve("u1", "小王住在哪里").items == []
    assert service.retrieve("u1", "我现在住在哪里").items


def test_english_predicate_lookup_rejects_a_named_subject():
    assert query_predicates("Where do I live?") == ["residence"]
    assert query_predicates("Where does Alice live?") == []

    service = MemoryService()
    service.ingest("u1", "我住在上海。", explicit=True)
    assert service.retrieve("u1", "Where does Alice live?").items == []


def test_long_cjk_run_is_retrievable_by_its_words():
    service = MemoryService()
    result = service.ingest("u1", "我喜欢咖啡并且每天早上喝咖啡", explicit=True)

    for query in ("咖啡", "喝咖啡", "每天早上"):
        items = service.retrieve("u1", query).items
        assert [item.memory.id for item in items] == result["memory_ids"], query


def test_long_cjk_query_requires_all_adjacent_bigrams():
    service = MemoryService()
    service.ingest("u1", "请记住我每天跑步", explicit=True)

    # A single common bigram ("每天") is not enough evidence for the much
    # more specific coffee question.
    assert service.retrieve("u1", "每天早上喝咖啡").items == []


def test_lexical_channel_discriminates_between_matches():
    class ConstantEmbedder:
        """Removes the dense channel as a confounder so lexical order shows."""

        model = "constant-test"
        dimensions = 2

        def embed(self, text: str) -> list[float]:
            return [1.0, 0.0]

    service = MemoryService(embedder=ConstantEmbedder())
    service.ingest("u1", "我喜欢咖啡。", explicit=True)
    full = service.ingest("u1", "我喜欢咖啡和茶。", explicit=True)

    items = service.retrieve("u1", "咖啡和茶").items
    # Both memories match, but only the one covering every query term may win.
    # With the previous constant bm25 mapping both scored exactly 1.0 and the
    # winner was decided by row order instead.
    assert [item.memory.id for item in items][0] == full["memory_ids"][0]


def test_lexical_projection_is_rebuildable():
    service = MemoryService()
    result = service.ingest("u1", "我喜欢咖啡并且每天早上喝咖啡", explicit=True)
    service.store.db.execute("DELETE FROM memory_fts")
    service.store.db.commit()
    assert service.store.db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0

    assert service.store.reindex_lexical() == 1
    items = service.retrieve("u1", "咖啡").items
    assert [item.memory.id for item in items] == result["memory_ids"]
    assert "bm25" in items[0].channels


def test_scoped_lexical_reindex_keeps_other_namespaces():
    service = MemoryService()
    service.ingest("u1", "我喜欢咖啡", explicit=True)
    service.ingest("u2", "我喜欢绿茶", explicit=True)

    assert service.store.reindex_lexical(namespace="u1") == 1
    assert service.retrieve("u2", "绿茶").items


# --------------------------------------------------------------------------- #
# Write path, lifecycle jobs and tenancy
# --------------------------------------------------------------------------- #

def test_duplicate_event_does_not_leave_an_open_transaction():
    store = SQLiteStore(":memory:")
    store.append_event(Event("e1", "u1", "message", {"text": "a"}, CHENGDU, idempotency_key="k"))
    duplicate = store.append_event(Event("e2", "u1", "message", {"text": "b"}, SHANGHAI, idempotency_key="k"))

    assert duplicate is False
    # The aborted transaction used to stay open, so the next commit would have
    # persisted a half-written event/outbox pair.
    assert store.db.in_transaction is False
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    store.close()


def test_existing_global_idempotency_schema_is_upgraded(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_type TEXT NOT NULL, "
        "payload TEXT NOT NULL, observed_at TEXT NOT NULL, occurred_from TEXT, occurred_to TEXT, "
        "source_message_id TEXT, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL)"
    )
    db.commit()
    db.close()

    store = SQLiteStore(str(db_path))
    assert store.append_event(Event("e1", "u1", "message", {"text": "a"}, CHENGDU,
                                    idempotency_key="same"))
    assert store.append_event(Event("e2", "u2", "message", {"text": "b"}, SHANGHAI,
                                    idempotency_key="same"))
    assert store.get_event_by_idempotency("u2", "same").id == "e2"


def test_projection_failure_rolls_back_and_retry_is_idempotent():
    from dive_memory.extraction import Candidate
    from dive_memory.gate import GateDecision

    class TwoCandidates:
        def extract(self, text, *, observed_at=None, explicit=False):
            decision = GateDecision(True, 0.8, 0.8, 0.8, "long_term", "test")
            return [
                Candidate("first", "semantic_fact", "FACT", {"predicate": "statement", "value": "first"},
                          decision, observed_at),
                Candidate("second", "semantic_fact", "FACT", {"predicate": "statement", "value": "second"},
                          decision, observed_at),
            ]

    class FailOnceOnSecondEmbedding:
        model = "fail-once"
        dimensions = 2

        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("transient embedding failure")
            return [1.0, 0.0]

    service = MemoryService(extractor=TwoCandidates(), embedder=FailOnceOnSecondEmbedding())
    with pytest.raises(RuntimeError, match="transient"):
        service.ingest("u1", "event", explicit=True)

    for table in ("memories", "memory_sources", "memory_vectors", "memory_fts"):
        assert service.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    failed = service.store.db.execute("SELECT status, attempts FROM outbox").fetchone()
    assert tuple(failed) == ("FAILED", 1)

    result = service.process_pending()
    assert len(result[0]["memory_ids"]) == 2
    for table in ("memories", "memory_sources", "memory_vectors", "memory_fts"):
        assert service.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2
    assert service.store.db.execute("SELECT status FROM outbox").fetchone()[0] == "DONE"


def test_supersession_closes_the_relation_validity_window():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at=CHENGDU)
    service.ingest("u1", "我住在上海", explicit=True, observed_at=SHANGHAI)

    valid_to = service.store.db.execute(
        "SELECT r.valid_to FROM relations r JOIN memory_relations mr ON mr.relation_id=r.id "
        "WHERE mr.memory_id=?", (first["memory_ids"][0],),
    ).fetchone()[0]
    assert valid_to == SHANGHAI


def test_hard_event_purge_removes_derived_memory_content():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    memory_id = result["memory_ids"][0]

    service.store.tombstone_event(result["event_id"], "retention", SHANGHAI, hard=True)

    assert service.get_event(result["event_id"]) is None
    assert service.get_memory(memory_id) is None
    assert service.store.db.execute("SELECT 1 FROM memory_fts WHERE memory_id=?", (memory_id,)).fetchone() is None
    assert service.store.db.execute("SELECT 1 FROM memory_vectors WHERE memory_id=?", (memory_id,)).fetchone() is None
    assert service.store.db.execute(
        "SELECT reason FROM tombstones WHERE object_id=?", (memory_id,)).fetchone()[0] == "retention"


def test_decay_accepts_a_naive_timestamp():
    service = MemoryService()
    memory = Memory(new_id("mem"), "u1", "episode", EvidenceState.FACT, "temporary plan",
                    {"predicate": "plan", "value": "temporary plan"}, MemoryStatus.ACTIVE,
                    durability="short_term", observed_at="2025-01-01T00:00:00+00:00")
    service.store.add_memory(memory)

    # Used to raise "can't compare offset-naive and offset-aware datetimes".
    assert service.decay("u1", now="2025-02-01T00:00:00")["archived"] == 1
    assert service.get_memory(memory.id).status.value == "ARCHIVED"


def test_decay_dry_run_does_not_touch_the_profile_projection():
    service = MemoryService()
    service.ingest("u1", "请记住我偏好 Python", explicit=True)
    service.store.db.execute("UPDATE profiles SET current_json=? WHERE namespace='u1'", ('{"sentinel": 1}',))
    service.store.db.commit()

    service.decay("u1", now="2026-01-01T00:00:00+00:00", dry_run=True)
    assert service.profile("u1")["sentinel"] == 1

    service.decay("u1", now="2026-01-01T00:00:00+00:00")
    assert service.profile("u1")["preference"] == "Python"


def test_correction_cannot_cross_namespaces():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶。", explicit=True)

    with pytest.raises(ValueError):
        service.correct_memory(result["memory_ids"][0], "我喜欢咖啡。", namespace="u2")

    # The rejected correction must not have deleted the original or written
    # into the other namespace.
    assert service.get_memory(result["memory_ids"][0]).status.value == "ACTIVE"
    assert service.list_memories("u2") == []


def test_correction_stays_within_its_own_namespace():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶。", explicit=True)
    corrected = service.correct_memory(first["memory_ids"][0], "我喜欢咖啡。", namespace="u1")

    old = service.get_memory(first["memory_ids"][0])
    new = service.get_memory(corrected["memory_ids"][0])
    assert old.status.value == "SUPERSEDED"
    assert new.namespace == "u1"
    assert new.supersedes_id == old.id
    assert new.version == 2
    assert service.profile("u1")["preference"] == "咖啡"


def test_stale_worker_claims_consume_retry_budget_and_orphans_are_removed():
    store = SQLiteStore(":memory:")
    event = Event("e-stale", "u1", "message", {"text": "remember"}, CHENGDU)
    assert store.append_event(event)
    assert [item.id for item in store.claim_pending_events()] == [event.id]

    for expected_attempts in (1, 2):
        store.db.execute("UPDATE outbox SET claimed_at='2000-01-01T00:00:00+00:00' WHERE event_id=?", (event.id,))
        store.db.commit()
        assert [item.id for item in store.claim_pending_events(stale_after_seconds=1)] == [event.id]
        assert store.db.execute("SELECT attempts FROM outbox WHERE event_id=?", (event.id,)).fetchone()[0] == expected_attempts

    store.db.execute("UPDATE outbox SET claimed_at='2000-01-01T00:00:00+00:00' WHERE event_id=?", (event.id,))
    store.db.execute(
        "INSERT INTO outbox(event_id,status,created_at) VALUES ('missing','PENDING',?)", (CHENGDU,),
    )
    store.db.commit()
    assert store.claim_pending_events(stale_after_seconds=1) == []
    row = store.db.execute("SELECT status,attempts FROM outbox WHERE event_id=?", (event.id,)).fetchone()
    assert tuple(row) == ("FAILED", 3)
    assert store.db.execute("SELECT 1 FROM outbox WHERE event_id='missing'").fetchone() is None


def test_replay_orders_offset_timestamps_by_actual_instant():
    service = MemoryService()
    service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T08:00:00+08:00")
    latest = service.ingest("u1", "我住在上海", explicit=True, observed_at="2025-01-01T00:30:00+00:00")

    report = service.rebuild_projections("u1")

    assert report["events"] == 2
    assert service.profile("u1")["residence"] == "上海"
    assert service.get_memory(latest["memory_ids"][0]).status == MemoryStatus.ACTIVE


def test_hard_purge_tombstones_remain_scoped_in_exports():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    other = service.ingest("u2", "请记住我喜欢绿茶", explicit=True)
    service.forget_event(first["event_id"], hard=True)
    service.forget_event(other["event_id"], hard=True)

    exported = service.export_namespace("u1")
    assert exported["events"] == []
    assert exported["memories"] == []
    assert {item["object_id"] for item in exported["tombstones"]} == {
        first["event_id"], first["memory_ids"][0],
    }
    assert {item["namespace"] for item in exported["tombstones"]} == {"u1"}


def test_terminal_memory_states_reject_correction_and_reinforcement():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True)
    second = service.correct_memory(first["memory_ids"][0], "我住在上海")
    old_id = first["memory_ids"][0]
    new_id_value = second["memory_ids"][0]

    for operation in (
        lambda: service.correct_memory(old_id, "我住在北京"),
        lambda: service.reinforce(old_id),
    ):
        with pytest.raises(ValueError, match="SUPERSEDED"):
            operation()

    service.forget(new_id_value)
    with pytest.raises(ValueError, match="DELETED"):
        service.correct_memory(new_id_value, "我住在北京")
    with pytest.raises(ValueError, match="DELETED"):
        service.reinforce(new_id_value)
    with pytest.raises(KeyError):
        service.forget("missing-memory")
    with pytest.raises(KeyError):
        service.forget_event("missing-event")
