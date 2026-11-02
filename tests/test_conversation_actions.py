"""Recent-conversation pin and delete stay inside the local evidence contract."""
import json
import threading

import pytest
from fastapi.testclient import TestClient
from wot.domain import DomainError
from wot.main import create_app
from wot.research import ResearchManager
from wot.service import WotService
from wot.storage import now


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    monkeypatch.setenv("WOT_MODEL", "mock-reflection-v1")
    value = WotService(tmp_path / "data")
    yield value
    assert value.provider.external_calls == 0
    value.close()


def two_rooms(service):
    older = service.conversation("先聊的那次", "skill_munger")
    newer = service.conversation("后来聊的", "skill_buffett")
    return older, newer


def test_pin_sorts_first_and_can_be_released(service):
    older, newer = two_rooms(service)
    assert [c["id"] for c in service.conversations()][:2] == [newer["id"], older["id"]]
    pinned = service.chat.pin(older["id"], True)
    assert pinned["pinned"] is True
    assert service.conversations()[0]["id"] == older["id"]
    assert service.chat.pin(older["id"], True)["pinned"] is True  # repeated pin is harmless
    assert service.chat.pin(older["id"], False)["pinned"] is False
    assert service.conversations()[0]["id"] == newer["id"]


def test_pin_rejects_non_boolean_and_missing_room(service):
    room = service.conversation("一个会话", "skill_munger")
    for value in ("true", 1, None):
        with pytest.raises(DomainError):
            service.chat.pin(room["id"], value)
    with pytest.raises(DomainError):
        service.chat.pin("conversation_missing", True)


def test_delete_hides_room_clears_text_and_is_idempotent(service):
    room = service.conversation("要删掉的会话", "skill_munger")
    service.send(room["id"], "这句不该留在本机", False, "general", "delete-once")
    service.chat.pin(room["id"], True)
    assert len(service.messages(room["id"])) == 2

    result = service.chat.remove(room["id"])
    assert result["status"] == "DELETED" and result["cleared_messages"] == 2
    assert room["id"] not in [c["id"] for c in service.conversations()]
    assert room["id"] not in [c["id"] for c in service.state()["conversations"]]
    assert room["id"] not in [c["id"] for c in service.export()["conversations"]]
    for message in service.messages(room["id"]):
        assert message["deleted"] is True
        assert message["text"] in {"[已删除]", "[关联来源已删除]"}
    assert all("这句不该留在本机" not in message["text"]
               for message in service.messages(room["id"]))

    # A second delete is a no-op rather than an error, matching a retried UI click.
    assert service.chat.remove(room["id"])["status"] == "DELETED"
    audit = service.store.fetchall(
        "SELECT resource_id FROM audit WHERE operation='CONVERSATION_DELETED'")
    assert [row["resource_id"] for row in audit] == [room["id"]]


def test_deleted_room_cannot_be_pinned_again(service):
    room = service.conversation("删掉再置顶", "skill_munger")
    service.chat.remove(room["id"])
    with pytest.raises(DomainError):
        service.chat.pin(room["id"], True)


def test_delete_is_refused_while_an_admitted_statement_depends_on_the_text(service):
    room = service.conversation("带记忆的会话", "skill_munger")
    sent = service.send(room["id"], "我确实更看重确定性", False, "general", "keep-once")
    evidence = service.admit(sent["user"]["id"])
    assert evidence["status"] == "ACTIVE"

    with pytest.raises(DomainError) as refusal:
        service.chat.remove(room["id"])
    assert "1 条已纳入长期记忆" in str(refusal.value)
    assert service.messages(room["id"])[0]["text"] == "我确实更看重确定性"

    service.delete(evidence["id"])
    assert service.chat.remove(room["id"])["status"] == "DELETED"


def test_routes_pin_and_delete_without_touching_other_rooms(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    with client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        kept = client.post("/api/v1/real-conversations",
                           json={"title": "留下的会话", "skill_id": "skill_munger"}).json()
        doomed = client.post("/api/v1/real-conversations",
                             json={"title": "删掉的会话", "skill_id": "skill_munger"}).json()
        assert client.put(f"/api/v1/real-conversations/{doomed['id']}/pin",
                          json={"pinned": True}).json()["pinned"] is True
        state = client.get("/api/v1/state").json()
        assert state["conversations"][0]["id"] == doomed["id"]
        assert client.put(f"/api/v1/real-conversations/{doomed['id']}/pin",
                          json={"pinned": "yes"}).status_code == 400
        assert client.put(f"/api/v1/real-conversations/{doomed['id']}/pin",
                          json={"pinned": True, "title": "越权字段"}).status_code == 400
        removed = client.delete(f"/api/v1/real-conversations/{doomed['id']}")
        assert removed.status_code == 200 and removed.json()["status"] == "DELETED"
        remaining = client.get("/api/v1/state").json()["conversations"]
        assert [item["id"] for item in remaining] == [kept["id"]]
        assert client.get(f"/api/v1/real-conversations/{doomed['id']}/messages").status_code == 200
        assert client.get(f"/api/v1/real-conversations/{doomed['id']}/updates").status_code == 410


def test_deleted_room_rejects_new_messages_preferences_presence_and_retry(service):
    room = service.conversation("已删除房间", "skill_munger")["id"]
    receipt = service.chat.accept(room, "只保存不要生成", False, "general", "before-delete")
    service.chat.remove(room)
    count = len(service.messages(room))
    for action in (
        lambda: service.chat.accept(room, "不能写回删除的房间", False, "general", "after-delete"),
        lambda: service.chat.web(room, False),
        lambda: service.chat.presence({"conversation_id": room, "tab_id": "deleted-tab", "visible": True, "composing": False}),
        lambda: service.chat.retry(receipt["batch"]["id"]),
    ):
        with pytest.raises(DomainError, match="已删除"):
            action()
    assert len(service.messages(room)) == count
    assert service.provider.calls == []


def test_remove_purges_completed_derivatives_and_invalidates_source_cards(service, tmp_path):
    private = "应从派生内容一起删除的独有句子_撤回核验"
    room = service.conversation(private, "skill_munger")["id"]
    sent = service.send(room, private, False, "general", "completed-private")
    batch = service.require(sent["user"]["reply_batch_id"], "reply_batch")
    actor = service.require(batch["agent_id"], "agent_instance")
    research = ResearchManager(service, tmp_path)
    service.research = research
    try:
        run = research.begin(scope_kind="CHAT", scope_id=room, agent_id=actor["id"],
                             generation_id=batch["generation_id"], version_id=batch["skill_version"],
                             source_message_ids=[sent["user"]["id"]])
        with service.store.transaction():
            batch.update(parts=[private], idle_followup=private, research_id=run)
            service.store.update(batch)
            actor.update(summary=[{"source_id": sent["user"]["id"], "text": private}],
                         checkpoint={"draft": private}, pending_messages=[{"text": private}])
            service.store.update(actor)
            record = service.require(run, "research_run")
            record.update(phase="COMPLETE", status="READY", sources=[{"id": "source-test", "excerpt": private}],
                          publications=[{"message_id": sent["agent"]["id"], "part_index": 0}])
            service.store.update(record)
            service.store.db.execute("UPDATE messages SET research_id=?,source_refs='[\"source-test\"]' WHERE id=?",
                                     (run, sent["agent"]["id"]))
        previous_revision = service.chat.updates(room)["revision"]
        assert research.sources(run)["sources"]
        service.chat.remove(room)
        with pytest.raises(DomainError):
            research.sources(run)
        assert service.require(room, "conversation")["redaction_revision"] > previous_revision
        with pytest.raises(DomainError) as gone:
            service.chat.updates(room, after_revision=previous_revision)
        assert gone.value.status == 410
        assert private not in "\n".join(service.store.db.iterdump())
        assert private not in json.dumps(service.export(), ensure_ascii=False)
        assert service.require(actor["id"], "agent_instance")["state"] == "INVALIDATED"
    finally:
        research.close()


def test_conversation_deletion_privacy_revision_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    directory = tmp_path / "restart-data"
    first = WotService(directory)
    room = first.conversation("会话", "skill_munger")["id"]
    first.chat.remove(room)
    revision = first.privacy_revision
    first.close()
    second = WotService(directory)
    try:
        assert second.privacy_revision == revision == 1
        assert second.conversations() == []
        assert second.provider.calls == []
    finally:
        second.close()


def test_startup_repairs_old_deleted_room_derivatives_without_new_deletion_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    directory = tmp_path / "legacy-deleted-data"
    private = "旧版删除遗留的派生原文_必须修复"
    first = WotService(directory)
    room = first.conversation("旧版会话", "skill_munger")["id"]
    receipt = first.send(room, private, False, "general", "legacy-deleted")
    with first.store.transaction():
        conversation = first.require(room, "conversation")
        conversation.update(deleted_at=now())
        first.store.update(conversation)
        first.store.db.execute("UPDATE messages SET text='[已删除]',deleted=1 WHERE conversation_id=?", (room,))
        first.store.audit("CONVERSATION_DELETED", room)
        batch = first.require(receipt["user"]["reply_batch_id"], "reply_batch")
        batch.update(parts=[private])
        first.store.update(batch)
        actor = first.require(batch["agent_id"], "agent_instance")
        actor.update(summary=[{"text": private}], checkpoint={"draft": private})
        first.store.update(actor)
    first.close()
    repaired = WotService(directory)
    try:
        assert private not in "\n".join(repaired.store.db.iterdump())
        assert repaired.require(room, "conversation")["deletion_cleanup_version"] == 1
        assert repaired.privacy_revision == 1
        assert len(repaired.store.fetchall("SELECT 1 FROM audit WHERE operation='CONVERSATION_DELETED'")) == 1
        assert repaired.provider.calls == []
    finally:
        repaired.close()


def test_deletion_discards_inflight_reply_and_does_not_restore_private_text(service):
    private = "删除进行中请求使用的独有原文"
    room = service.conversation("正在生成", "skill_munger")["id"]
    receipt = service.chat.accept(room, private, False, "general", "inflight-delete")
    rid = receipt["batch"]["id"]
    started, release = threading.Event(), threading.Event()
    original = service.provider.generate

    def blocked(**kwargs):
        result = original(**kwargs)
        started.set()
        assert release.wait(5)
        return result

    service.provider.generate = blocked
    with service.store.transaction():
        batch = service.require(rid, "reply_batch")
        batch["status"] = "GENERATING"
        service.store.update(batch)
    worker = threading.Thread(target=service.chat._generate, args=(rid, batch["epoch"]))
    worker.start()
    try:
        assert started.wait(5)
        service.chat.remove(room)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    service.chat._tick(dispatch=False)
    assert len(service.messages(room)) == 1
    assert service.messages(room)[0]["deleted"]
    assert private not in "\n".join(service.store.db.iterdump())
    assert service.store.all("agent_call")[0]["discarded"] is True
    assert service.provider.calls == []
