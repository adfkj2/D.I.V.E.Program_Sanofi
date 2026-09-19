from dive_memory.service import MemoryService
from dive_memory.worker import OutboxWorker
from concurrent.futures import ThreadPoolExecutor


def test_ephemeral_text_is_not_persisted():
    service = MemoryService()
    result = service.ingest("u1", "我今天晚上想看电影")
    assert result["memory_ids"] == []
    assert service.retrieve("u1", "电影").items == []


def test_sensitive_secret_is_rejected_and_write_decision_is_auditable():
    service = MemoryService()
    result = service.ingest("u1", "请记住我的 API key 是 secret-value", explicit=True)

    assert result["memory_ids"] == []
    decision = service.store.db.execute(
        "SELECT accepted, reason, extractor_version FROM write_decisions WHERE event_id=?",
        (result["event_id"],),
    ).fetchone()
    assert decision["accepted"] == 0
    assert decision["reason"] == "sensitive data rejected"
    assert decision["extractor_version"] == "HeuristicExtractionProvider"


def test_explicit_memory_has_provenance():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert len(result["memory_ids"]) == 1
    memory = service.get_memory(result["memory_ids"][0])
    assert memory is not None
    assert memory.source_event_ids == [result["event_id"]]
    assert memory.evidence_state.value == "FACT"
    decision = service.store.db.execute(
        "SELECT accepted, reason FROM write_decisions WHERE event_id=?", (result["event_id"],),
    ).fetchone()
    assert tuple(decision) == (1, "accepted by write gate")


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


def test_forget_cleans_relation_and_entity_projections():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    memory_id = result["memory_ids"][0]
    assert service.store.db.execute("SELECT 1 FROM memory_relations WHERE memory_id=?", (memory_id,)).fetchone()
    assert service.store.db.execute("SELECT 1 FROM memory_entities WHERE memory_id=?", (memory_id,)).fetchone()
    service.forget(memory_id)
    assert service.store.db.execute("SELECT 1 FROM memory_relations WHERE memory_id=?", (memory_id,)).fetchone() is None
    assert service.store.db.execute("SELECT 1 FROM memory_entities WHERE memory_id=?", (memory_id,)).fetchone() is None
    assert service.store.db.execute("SELECT 1 FROM relations").fetchone() is None
    assert service.store.db.execute("SELECT 1 FROM entities").fetchone() is None


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


def test_taxonomy_classifies_preference_procedure_episode_and_resource():
    service = MemoryService()
    examples = {
        "我喜欢绿茶": "preference",
        "请记住发布流程步骤是先测试再部署": "procedural",
        "请记住上次部署因权限失败": "episode",
        "请记住文档是 https://example.com/guide": "resource",
        "请记住2025年1月2日发布版本": "temporal_event",
    }
    for text, expected_kind in examples.items():
        result = service.ingest("u1", text, explicit=True)
        assert service.get_memory(result["memory_ids"][0]).kind == expected_kind


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
    survivor = service.get_memory(first["memory_ids"][0])
    assert set(survivor.source_event_ids) == {first["event_id"], second["event_id"]}
    assert service.store.db.execute(
        "SELECT 1 FROM memory_vectors WHERE memory_id=?", (second["memory_ids"][0],),
    ).fetchone() is None


def test_context_builder_obeys_budget_and_planner_intent():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    result = service.retrieve_context("u1", "现在喜欢什么", token_budget=20)
    assert result["plan"]["intent"] == "current"
    assert result["plan"]["token_budget"] == 20
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


def test_memory_mode_survives_restart(tmp_path):
    db_path = str(tmp_path / "memory-mode.sqlite3")
    first = MemoryService(db_path)
    first.set_memory_enabled("u1", False)
    first.store.close()

    second = MemoryService(db_path)
    result = second.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    assert result["memory_disabled"] is True
    assert result["memory_ids"] == []


def test_namespaces_are_isolated():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    service.ingest("u2", "请记住我喜欢绿茶", explicit=True)
    assert service.retrieve("u1", "绿茶").items == []
    assert service.retrieve("u2", "咖啡").items == []


def test_service_bounds_negative_paging_and_context_budget():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    assert service.retrieve("u1", "咖啡", limit=-10).items
    assert service.list_memories("u1", offset=-10)
    context = service.retrieve_context("u1", "咖啡", token_budget=-10)
    assert context["estimated_tokens"] == 0


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


def test_two_store_connections_cannot_claim_the_same_event(tmp_path):
    db_path = str(tmp_path / "claims.sqlite3")
    first = MemoryService(db_path)
    created = first.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    second = MemoryService(db_path)

    assert [event.id for event in first.store.claim_pending_events()] == [created["event_id"]]
    assert second.store.claim_pending_events() == []


def test_forget_deferred_event_prevents_late_projection():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    service.forget_event(result["event_id"])
    assert service.pending_events() == []
    assert service.process_pending() == []
    assert service.list_memories("u1", status="ACTIVE") == []


def test_hard_event_purge_removes_event_and_outbox_row():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    service.store.tombstone_event(result["event_id"], "retention", "2026-01-01T00:00:00+00:00", hard=True)
    assert service.store.get_event(result["event_id"]) is None
    assert service.pending_events() == []


def test_hard_purge_reassigns_relation_when_memory_has_another_source():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    second = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    service.consolidate("u1")
    survivor_id = first["memory_ids"][0]

    service.forget_event(first["event_id"], hard=True)

    survivor = service.get_memory(survivor_id)
    assert survivor.status.value == "ACTIVE"
    assert survivor.source_event_ids == [second["event_id"]]
    relation = service.store.db.execute(
        "SELECT r.source_event_id FROM relations r JOIN memory_relations mr ON mr.relation_id=r.id "
        "WHERE mr.memory_id=?", (survivor_id,),
    ).fetchone()
    assert relation["source_event_id"] == second["event_id"]


def test_disabling_memory_before_deferred_worker_suppresses_projection():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, defer=True)
    service.set_memory_enabled("u1", False)
    processed = service.process_pending()
    assert processed == [{"event_id": result["event_id"], "memory_ids": [], "memory_disabled": True}]
    assert service.list_memories("u1", status="ACTIVE") == []


def test_idempotency_returns_original_event_id():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, idempotency_key="req-1")
    second = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, idempotency_key="req-1")
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]


def test_idempotency_keys_are_scoped_by_namespace():
    service = MemoryService()
    first = service.ingest("u1", "请记住我喜欢咖啡", explicit=True, idempotency_key="same-key")
    second = service.ingest("u2", "请记住我喜欢绿茶", explicit=True, idempotency_key="same-key")
    assert first["event_id"] != second["event_id"]
    assert second["duplicate"] is False


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
    old = service.get_memory(first["memory_ids"][0])
    new = service.get_memory(corrected["memory_ids"][0])
    assert old.status.value == "SUPERSEDED"
    assert new.supersedes_id == old.id
    assert new.version == 2


def test_projection_replay_preserves_ids_versions_and_tombstones():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True,
                           observed_at="2025-01-01T00:00:00+00:00")
    second = service.ingest("u1", "我住在上海", explicit=True,
                            observed_at="2026-01-01T00:00:00+00:00")
    forgotten = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    service.forget(forgotten["memory_ids"][0])

    report = service.rebuild_projections("u1")

    assert report == {"namespace": "u1", "events": 3, "memories": 2, "dry_run": False}
    assert service.get_memory(first["memory_ids"][0]).status.value == "SUPERSEDED"
    latest = service.get_memory(second["memory_ids"][0])
    assert latest.version == 2
    assert latest.supersedes_id == first["memory_ids"][0]
    assert service.get_memory(forgotten["memory_ids"][0]) is None
    assert service.retrieve("u1", "绿茶").items == []


def test_memory_version_audit_is_written_for_each_projection():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True)
    second = service.correct_memory(first["memory_ids"][0], "我住在上海")

    old_versions = service.memory_versions(first["memory_ids"][0])
    new_versions = service.memory_versions(second["memory_ids"][0])
    assert old_versions[0]["version"] == 1
    assert old_versions[0]["reason"] == "created"
    assert [(item["version"], item["reason"]) for item in new_versions] == [
        (1, "created"), (2, "supersession"),
    ]


def test_forget_refreshes_profile_projection():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert service.profile("u1")["preference"] == "绿茶"
    service.forget(result["memory_ids"][0])
    assert "preference" not in service.profile("u1")


def test_archive_refreshes_profile_projection():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    memory_id = result["memory_ids"][0]
    service.store.db.execute(
        "UPDATE memories SET valid_to=? WHERE id=?",
        ("2025-01-15T00:00:00+00:00", memory_id),
    )
    service.store.db.commit()
    assert service.profile("u1")["preference"] == "绿茶"
    assert service.archive_expired("u1", now="2025-02-01T00:00:00+00:00") == 1
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


def test_reinforcement_restores_archived_memory_and_jobs_are_auditable():
    from dive_memory.ids import new_id
    from dive_memory.models import EvidenceState, Memory, MemoryStatus

    service = MemoryService()
    memory = Memory(new_id("mem"), "u1", "episode", EvidenceState.FACT, "temporary plan",
                    {"predicate": "plan", "value": "temporary plan"}, MemoryStatus.ACTIVE,
                    importance=0.4, confidence=0.6, durability="short_term",
                    observed_at="2025-01-01T00:00:00+00:00")
    service.store.add_memory(memory)
    service.decay("u1", now="2025-02-01T00:00:00+00:00")

    restored = service.reinforce(memory.id, namespace="u1")

    assert restored.status.value == "REINFORCED"
    assert restored.importance > 0.4
    assert service.retrieve("u1", "temporary plan").items
    jobs = service.jobs("u1")
    assert jobs[0]["job_type"] == "decay"
    assert jobs[0]["status"] == "DONE"
    assert jobs[0]["output_summary"]["archived"] == 1


def test_historical_query_can_retrieve_superseded_memory():
    service = MemoryService()
    first = service.ingest("u1", "我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    service.ingest("u1", "我住在上海", explicit=True, observed_at="2026-01-01T00:00:00+00:00")
    result = service.retrieve("u1", "以前住成都")
    assert result.items and result.items[0].memory.id == first["memory_ids"][0]
