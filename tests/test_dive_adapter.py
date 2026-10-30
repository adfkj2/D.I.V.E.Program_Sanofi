import json
from hashlib import sha256
from pathlib import Path

import pytest
from wot.dive_adapter import DiveMemoryAdapter

NS = "user/local/real"
NOW = "2026-10-30T12:00:00+08:00"


def write(adapter, text="  我希望更果断，昨天因为犹豫错过了投递。  ", **overrides):
    options = dict(key="source-1", message_id="message-1", observed_at=NOW)
    options.update(overrides)
    return adapter.ingest(NS, text, **options)


def test_chinese_verbatim_is_an_observation_with_real_provenance(tmp_path):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    try:
        recorded = write(adapter)
        found = adapter.retrieve(NS, "犹豫")
        assert len(found) == 1
        assert found[0]["text"] == "  我希望更果断，昨天因为犹豫错过了投递。  "
        assert found[0]["event_ids"] == [recorded["event_id"]]
        assert found[0]["memory_id"] in recorded["memory_ids"]
        assert found[0]["evidence_state"] == "OBSERVATION"
        assert adapter._service.profile(NS).get("decisive") is None
    finally:
        adapter.close()


def test_idempotent_retry_and_changed_payload_conflict(tmp_path):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    try:
        original = write(adapter)
        assert write(adapter) == original
        assert len(adapter._service.store.all_events()) == 1
        with pytest.raises(ValueError, match="Idempotency"):
            write(adapter, "不同的中文来源")
        with pytest.raises(ValueError, match="Idempotency"):
            write(adapter, message_id="another-message")
    finally:
        adapter.close()


@pytest.mark.parametrize("namespace", [
    "user/local/simulated", "user/local/chosen", "user/local/pfe_private",
    "user/local/real/extra", "user/../real", "user/a b/real", "user/中文/real",
    "user//real", "user/local/real\n", "user/local/external_skill",
])
def test_untrusted_namespace_is_rejected_before_core_write(tmp_path, namespace):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    try:
        with pytest.raises(ValueError, match="namespace"):
            adapter.ingest(namespace, "我喜欢猫", key="k", message_id="m", observed_at=NOW)
        with pytest.raises(ValueError, match="namespace"):
            adapter.retrieve(namespace, "猫")
        assert not adapter._service.store.all_events()
    finally:
        adapter.close()


def test_cross_user_retrieval_and_deletion_are_scoped(tmp_path):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    try:
        event = write(adapter)
        assert adapter.retrieve("user/other/real", "犹豫") == []
        with pytest.raises(KeyError):
            adapter.delete("user/other/real", event["event_id"])
        assert adapter.retrieve(NS, "犹豫")
    finally:
        adapter.close()


def test_delete_survives_restart_reindex_and_ingest_retry(tmp_path):
    path = tmp_path / "memory.db"
    adapter = DiveMemoryAdapter(path)
    event = write(adapter)
    adapter.delete(NS, event["event_id"])
    adapter.delete(NS, event["event_id"])
    assert adapter._service.get_event(event["event_id"]) is None
    adapter.close()
    adapter = DiveMemoryAdapter(path)
    try:
        adapter._service.rebuild_projections()
        adapter._service.reindex()
        assert adapter.retrieve(NS, "犹豫") == []
        with pytest.raises(ValueError, match="Deleted"):
            write(adapter)
        assert not adapter._service.store.all_events()
    finally:
        adapter.close()


@pytest.mark.parametrize("already_written", [False, True])
def test_delete_by_key_blocks_unacknowledged_and_future_core_writes(tmp_path, already_written):
    path = tmp_path / "memory.db"
    adapter = DiveMemoryAdapter(path)
    if already_written:
        write(adapter)
    adapter.delete_by_key(NS, "source-1")
    adapter.close()
    adapter = DiveMemoryAdapter(path)
    try:
        with pytest.raises(ValueError, match="Deleted"):
            write(adapter)
        assert adapter.retrieve(NS, "犹豫") == []
        assert not adapter._service.store.all_events()
    finally:
        adapter.close()


def test_projection_failure_retries_core_outbox_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    adapter = DiveMemoryAdapter(path)
    original = adapter._service._process_event

    def fail_after_projection(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(adapter._service, "_process_event", fail_after_projection)
    with pytest.raises(RuntimeError, match="projection failed"):
        write(adapter)
    assert len(adapter._service.store.all_events()) == 1
    assert adapter.retrieve(NS, "犹豫") == []
    adapter.close()
    restarted = DiveMemoryAdapter(path)
    try:
        recovered = write(restarted)
        assert recovered["memory_ids"]
        assert len(restarted._service.store.all_events()) == 1
        assert len(restarted.retrieve(NS, "犹豫")) == 1
        row = restarted._service.store.db.execute("SELECT status FROM outbox").fetchone()
        assert row["status"] == "DONE"
    finally:
        restarted.close()


def test_repeated_projection_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    monkeypatch.setattr(adapter._service, "_process_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fault")))
    try:
        for _ in range(4):
            with pytest.raises(RuntimeError, match="projection"):
                write(adapter)
        assert adapter.retrieve(NS, "犹豫") == []
    finally:
        adapter.close()


def test_stale_core_worker_claim_is_recovered_on_retry(tmp_path):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    try:
        event = adapter._service.ingest(
            NS, "  我希望更果断，昨天因为犹豫错过了投递。  ",
            idempotency_key="source-1", source_message_id="message-1",
            observed_at=NOW, source_type="user", defer=True,
        )
        with adapter._service.store.transaction():
            adapter._service.store.db.execute(
                "UPDATE outbox SET status='PROCESSING', claimed_at=? WHERE event_id=?",
                ("2026-01-01T00:00:00+00:00", event["event_id"]),
            )
        assert write(adapter)["memory_ids"]
        assert adapter.retrieve(NS, "犹豫")
    finally:
        adapter.close()


def test_distinct_identical_utterances_keep_distinct_evidence(tmp_path):
    adapter = DiveMemoryAdapter(tmp_path / "memory.db")
    try:
        first = write(adapter)
        second = write(adapter, key="source-2", message_id="message-2")
        assert first["memory_ids"] != second["memory_ids"]
        assert len(adapter.retrieve(NS, "犹豫")) == 2
        adapter.delete(NS, first["event_id"])
        assert len(adapter.retrieve(NS, "犹豫")) == 1
    finally:
        adapter.close()


def test_vendored_files_match_pinned_hashes_and_exclude_old_product():
    vendor = Path(__file__).parents[1] / "vendor/dive-memory"
    provenance = json.loads((vendor / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["revision"] == "eb9898b0a4e4965d8e9d276dc2a76b500461fb68"
    for item in provenance["copied_files"]:
        assert sha256((vendor / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    core = vendor / "src/dive_memory"
    assert not any((core / path).exists() for path in (
        "application", "agent", "web.py", "api.py", "cli.py", "static", "oracle", "eval_adapters"
    ))
