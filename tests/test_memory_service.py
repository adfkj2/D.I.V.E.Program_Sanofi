from dive_memory.service import MemoryService
from dive_memory.worker import OutboxWorker
from concurrent.futures import ThreadPoolExecutor


def test_ephemeral_text_is_not_persisted():
    service = MemoryService()
    result = service.ingest("u1", "我今天晚上想看电影")
    assert result["memory_ids"] == []
    assert service.retrieve("u1", "电影").items == []


def test_explicit_memory_has_provenance():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert len(result["memory_ids"]) == 1
    memory = service.get_memory(result["memory_ids"][0])
    assert memory is not None
    assert memory.source_event_ids == [result["event_id"]]
    assert memory.evidence_state.value == "FACT"


def test_new_fact_supersedes_old_fact_but_old_event_remains():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    second = service.ingest("u1", "我住在上海", explicit=True, observed_at="2026-03-01T00:00:00+00:00")
    old = service.get_memory(first["memory_ids"][0])
    new = service.get_memory(second["memory_ids"][0])
    assert old.status.value == "SUPERSEDED"
    assert new.supersedes_id == old.id
    assert old.valid_to == "2026-03-01T00:00:00+00:00"
    assert service.store.get_event(first["event_id"]) is not None


def test_retrieval_and_forget_remove_memory_from_active_search():
    service = MemoryService()
    result = service.ingest("u1", "请记住我偏好 Python", explicit=True)
    memory_id = result["memory_ids"][0]
    assert service.retrieve("u1", "Python").items[0].memory.id == memory_id
    service.forget(memory_id)
    assert service.retrieve("u1", "Python").items == []


def test_as_of_query_returns_historical_version():
    service = MemoryService()
    old = service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    service.ingest("u1", "我住在上海", explicit=True, observed_at="2026-03-01T00:00:00+00:00")
    historical = service.timeline("u1", as_of="2025-06-01T00:00:00+00:00")
    assert [m.id for m in historical] == old["memory_ids"]


def test_explicit_date_is_kept_as_validity_start():
    service = MemoryService()
    result = service.ingest("u1", "2025年1月2日我住在成都", explicit=True)
    memory = service.get_memory(result["memory_ids"][0])
    assert memory.valid_from == "2025-01-02T00:00:00+00:00"


def test_consolidation_is_dry_run_before_mutation():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    second = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    report = service.consolidate("u1", dry_run=True)
    assert report.examined == 2
    assert report.merged == 1
    assert service.get_memory(second["memory_ids"][0]).status.value == "ACTIVE"
    service.consolidate("u1", dry_run=False)
    assert service.get_memory(second["memory_ids"][0]).status.value == "MERGED"


def test_context_builder_obeys_budget_and_planner_intent():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    result = service.retrieve_context("u1", "现在喜欢什么", token_budget=20)
    assert result["plan"]["intent"] == "current"
    assert result["estimated_tokens"] <= 20


def test_event_forget_propagates_to_derived_memory():
    service = MemoryService()
    result = service.ingest("u1", "请记住我偏好 Python", explicit=True)
    deleted = service.forget_event(result["event_id"])
    assert result["memory_ids"][0] in deleted
    assert service.get_memory(result["memory_ids"][0]).status.value == "DELETED"
    assert service.store.get_event(result["event_id"]) is not None


def test_user_can_disable_new_memory_writes():
    service = MemoryService()
    service.set_memory_enabled("u1", False)
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    assert result["memory_ids"] == []
    assert result["memory_disabled"] is True
    assert service.pending_events() == []
    assert service.process_pending() == []


def test_event_is_published_to_outbox_until_processed():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    assert [event.id for event in service.pending_events()] == [result["event_id"]]
    service.mark_event_processed(result["event_id"])
    assert service.pending_events() == []


def test_synchronous_ingest_does_not_reprocess_outbox():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    assert service.pending_events() == []
    assert service.process_pending() == []
    assert len(service.list_memories("u1", status="ACTIVE")) == 1


def test_file_backed_store_survives_restart(tmp_path):
    db_path = str(tmp_path / "memory.sqlite3")
    first = MemoryService(db_path)
    result = first.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    first.store.close()
    second = MemoryService(db_path)
    assert second.get_memory(result["memory_ids"][0]) is not None
    assert second.retrieve("u1", "咖啡").items


def test_namespaces_are_isolated():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    service.ingest("u2", "请记住我喜欢绿茶", explicit=True)
    assert service.retrieve("u1", "绿茶").items == []
    assert service.retrieve("u2", "咖啡").items == []


def test_concurrent_ingest_is_serialized_for_http_worker_threads():
    service = MemoryService()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: service.ingest("u1", "请记住我喜欢咖啡", explicit=True), range(20)
        ))
    assert len({result["event_id"] for result in results}) == 20
    assert len(service.list_memories("u1", status="ACTIVE")) == 20


def test_deferred_event_is_processed_by_outbox_worker():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    assert result["memory_ids"] == []
    processed = service.process_pending()
    assert processed[0]["event_id"] == result["event_id"]
    assert processed[0]["memory_ids"]
    assert service.pending_events() == []


def test_outbox_worker_drains_deferred_events():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    report = OutboxWorker(service, batch_size=1).drain()
    assert report.processed == 1
    assert report.results[0]["memory_ids"]
    assert OutboxWorker(service).run_once().processed == 0


def test_forget_deferred_event_prevents_late_projection():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    service.forget_event(result["event_id"])
    assert service.pending_events() == []
    assert service.process_pending() == []
    assert service.list_memories("u1", status="ACTIVE") == []


def test_idempotency_returns_original_event_id():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, idempotency_key="req-1")
    second = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, idempotency_key="req-1")
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]


def test_entity_resolution_adds_entity_retrieval_signal():
    service = MemoryService()
    service.ingest("u1", "请记住我住在成都", explicit=True)
    result = service.retrieve("u1", "成都")
    assert result.items
    assert "entity" in result.items[0].channels


def test_relation_table_materializes_user_preference_edge():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    row = service.store.db.execute("SELECT predicate FROM relations WHERE namespace='u1'").fetchone()
    assert row["predicate"] == "prefers"
    result = service.retrieve("u1", "prefers")
    assert result.items and "relation" in result.items[0].channels


def test_profile_is_current_projection_and_correction_creates_new_event():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert service.profile("u1")["preference"] == "绿茶"
    corrected = service.correct_memory(first["memory_ids"][0], "我喜欢咖啡")
    assert corrected["memory_ids"]
    assert service.profile("u1")["preference"] == "咖啡"
    assert service.get_memory(first["memory_ids"][0]).status.value == "DELETED"


def test_forget_refreshes_profile_projection():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert service.profile("u1")["preference"] == "绿茶"
    service.forget(result["memory_ids"][0])
    assert "preference" not in service.profile("u1")


def test_decay_archives_expired_short_term_memory():
    from dive_memory.ids import new_id
    from dive_memory.models import EvidenceState, Memory, MemoryStatus

    service = MemoryService()
    memory = Memory(new_id("mem"), "u1", "episode", EvidenceState.FACT, "temporary plan",
                    {"predicate": "plan", "value": "temporary plan"}, MemoryStatus.ACTIVE,
                    durability="short_term", observed_at="2025-01-01T00:00:00+00:00")
    service.store.add_memory(memory)
    result = service.decay("u1", now="2025-02-01T00:00:00+00:00")
    assert result["archived"] == 1
    assert service.get_memory(memory.id).status.value == "ARCHIVED"


def test_historical_query_can_retrieve_superseded_memory():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    service.ingest("u1", "我住在上海", explicit=True, observed_at="2026-01-01T00:00:00+00:00")
    result = service.retrieve("u1", "以前住成都")
    assert result.items and result.items[0].memory.id == first["memory_ids"][0]
