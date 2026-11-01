"""Ordinary-chat burst publication, cancellation, privacy and durable retries."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from wot.domain import DomainError
from wot.main import create_app
from wot.model import Provider
from wot.service import WotService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 31, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class BurstProvider(Provider):
    def __init__(self, parts=None, followup=None):
        super().__init__()
        self.parts = parts or ["这句我听明白了。", "不过，刚才那件事你是怎么想的？", "先说说当时发生了什么。"]
        self.followup = followup

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"text": json.dumps({"messages": self.parts, "idle_followup": self.followup}, ensure_ascii=False),
                "tokens": 60, "provider": "mock", "cost": 0}


@pytest.fixture
def chat(tmp_path):
    provider = BurstProvider()
    service = WotService(tmp_path / "data", provider)
    clock = Clock()
    engine = service.chat
    engine.clock = clock
    room = service.conversation("自然聊天", "skill_feynman")["id"]
    yield service, engine, provider, room, clock
    service.close()


def accept(chat, text="今天有件事", key="first", remember=False):
    service, engine, provider, room, clock = chat
    return engine.accept(room, text, remember, "general", key)


def generate(chat, rid):
    service, engine, provider, room, clock = chat
    with engine.lock, service.store.transaction():
        batch = service.require(rid, "reply_batch")
        batch["status"] = "GENERATING"
        engine._save(batch)
        engine.inflight.add(rid)
    engine._generate(rid, batch["epoch"])


def test_parts_publish_separately_with_stable_identity_and_only_public_context(chat):
    service, engine, provider, room, clock = chat
    receipt = accept(chat)
    rid = receipt["batch"]["id"]
    assert receipt["batch"]["typing"] is True
    generate(chat, rid)
    assert len(service.messages(room)) == 1
    assert "parts" not in engine.updates(room)["batch"]
    engine._tick(dispatch=False)
    first = service.messages(room)
    assert len(first) == 2
    assert first[-1]["part_index"] == 0
    assert first[-1]["client_request_id"] == "first"
    assert service.conversations()[0]["last_message_preview"] == provider.parts[0]
    clock.advance(0.5)
    engine._tick(dispatch=False)
    assert len(service.messages(room)) == 2
    for _ in range(2):
        clock.advance(4)
        engine._tick(dispatch=False)
    messages = service.messages(room)
    assert [m["part_index"] for m in messages[1:]] == [0, 1, 2]
    assert len({m["id"] for m in messages}) == 4
    assert [m["sequence"] for m in messages] == [1, 2, 3, 4]
    assert engine.public(service.require(rid, "reply_batch"))["typing"] is False
    assert len(provider.calls) == 1
    assert len(service.store.all("chat_generation")) == 1
    engine.retry(rid)
    engine._tick(dispatch=False)
    assert len(service.messages(room)) == 4


def test_new_message_cancels_unpublished_tail_and_coalesces_user_turns(chat):
    service, engine, provider, room, clock = chat
    original = accept(chat)
    rid = original["batch"]["id"]
    generate(chat, rid)
    engine._tick(dispatch=False)
    newer = accept(chat, "其实我改主意了", "second", True)
    later = accept(chat, "再补充一个条件", "third")
    assert service.require(rid, "reply_batch")["status"] == "CANCELLED"
    assert service.require(rid, "reply_batch")["parts"] == []
    assert service.require(newer["batch"]["id"], "reply_batch")["status"] == "CANCELLED"
    batch = service.require(later["batch"]["id"], "reply_batch")
    assert set(batch["source_message_ids"]) == {original["user"]["id"], newer["user"]["id"], later["user"]["id"]}
    generate(chat, batch["id"])
    assert "其实我改主意了" in provider.calls[-1]["topic"]
    assert provider.parts[1] not in str(provider.calls[-1]["transcript"])
    assert len(service.active_evidence()) == 1
    assert service.active_evidence()[0]["text"] == "其实我改主意了"
    with pytest.raises(DomainError):
        engine.retry(rid)


def test_debounce_prevents_generation_before_quiet_window(chat):
    service, engine, provider, room, clock = chat
    receipt = accept(chat)
    engine._tick()
    assert provider.calls == []
    clock.advance(0.79)
    engine._tick()
    assert provider.calls == []
    assert service.require(receipt["batch"]["id"], "reply_batch")["status"] == "QUEUED"


def test_stale_inflight_result_never_publishes_but_records_incurred_usage(chat):
    service, engine, provider, room, clock = chat
    receipt = accept(chat)
    started, release = threading.Event(), threading.Event()
    original = provider.generate

    def slow(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    provider.generate = slow
    thread = threading.Thread(target=generate, args=(chat, receipt["batch"]["id"]))
    thread.start()
    assert started.wait(5)
    newer = accept(chat, "等等，是另一回事", "interrupt")
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert service.require(receipt["batch"]["id"], "reply_batch")["status"] == "CANCELLED"
    engine._tick(dispatch=False)
    assert all(m["speaker"] == "USER" for m in service.messages(room))
    assert service.store.all("chat_generation")[0]["discarded"] is True
    assert engine.updates(room)["batch"]["id"] == newer["batch"]["id"]


def test_idle_followup_uses_same_generation_and_current_presence(chat):
    service, engine, provider, room, clock = chat
    provider.parts = ["我想起另一件小事。"]
    provider.followup = "那天还有个细节。"
    engine.idle_interval = (20, 20)
    receipt = accept(chat)
    engine.presence({"conversation_id": room, "tab_id": "tab", "visible": True, "composing": False})
    generate(chat, receipt["batch"]["id"])
    engine._tick(dispatch=False)
    assert engine.updates(room)["batch"]["status"] == "WAITING_FOLLOWUP"
    for _ in range(4):
        clock.advance(5)
        engine.presence({"conversation_id": room, "tab_id": "tab", "visible": True, "composing": False})
    engine._tick(dispatch=False)
    assert service.messages(room)[-1]["text"] == provider.followup
    assert len(provider.calls) == 1
    assert engine.updates(room)["batch"]["status"] == "COMPLETED"
    clock.advance(60)
    engine._tick(dispatch=False)
    assert len(service.messages(room)) == 3


@pytest.mark.parametrize("mode", ["leave", "compose", "expire"])
def test_idle_followup_cancelled_by_leaving_typing_or_stale_presence(chat, mode):
    service, engine, provider, room, clock = chat
    provider.parts, provider.followup = ["刚才的话我接住了。"], "还有个小细节。"
    engine.presence({"conversation_id": room, "tab_id": "tab", "visible": True, "composing": False})
    rid = accept(chat)["batch"]["id"]
    generate(chat, rid)
    engine._tick(dispatch=False)
    if mode != "expire":
        engine.presence({"conversation_id": room if mode == "compose" else None,
                         "tab_id": "tab", "visible": mode == "compose", "composing": mode == "compose"})
    clock.advance(40)
    engine._tick(dispatch=False)
    assert len(service.messages(room)) == 2
    assert engine.updates(room)["batch"]["status"] == "COMPLETED"


def test_deletion_purges_pending_text_and_returns_redaction_reset(chat):
    service, engine, provider, room, clock = chat
    receipt = accept(chat, remember=True)
    rid = receipt["batch"]["id"]
    generate(chat, rid)
    engine._tick(dispatch=False)
    before = engine.updates(room)
    evidence = service.active_evidence()[0]
    service.delete(evidence["id"])
    after = engine.updates(room, after_sequence=2, after_revision=before["revision"])
    assert after["reset"] is True
    assert len(after["messages"]) == 2
    assert all(m["deleted"] for m in after["messages"])
    assert service.require(rid, "reply_batch")["parts"] == []
    assert provider.parts[0] not in json.dumps(service.store.all("reply_batch"), ensure_ascii=False)
    assert "关联来源" in service.conversations()[0]["last_message_preview"] or service.conversations()[0]["last_message_preview"] == "[关联来源已删除]"
    with pytest.raises(DomainError):
        engine.retry(rid)


def test_restart_stops_without_model_and_retry_resumes_generated_tail(tmp_path):
    directory = tmp_path / "restart"
    provider = BurstProvider()
    service = WotService(directory, provider)
    engine = service.chat
    clock = Clock()
    engine.clock = clock
    room = service.conversation("重启", "skill_munger")["id"]
    rid = engine.accept(room, "说说这件事", False, "general", "restart")["batch"]["id"]
    generate((service, engine, provider, room, clock), rid)
    engine._tick(dispatch=False)
    first_id = service.messages(room)[-1]["id"]
    service.close()
    new_provider = BurstProvider()
    newer = WotService(directory, new_provider)
    try:
        assert new_provider.calls == []
        assert newer.chat.updates(room)["batch"]["status"] == "INTERRUPTED"
        newer.chat.retry(rid)
        newer.chat.part_interval = (0, 0)
        for _ in range(3):
            newer.chat._tick(dispatch=False)
        assert len(newer.messages(room)) == 4
        assert newer.messages(room)[1]["id"] == first_id
        assert new_provider.calls == []
    finally:
        newer.close()


def test_malformed_protocol_is_failure_without_publishing_json_and_keeps_usage(chat):
    service, engine, provider, room, clock = chat
    provider.generate = lambda **kwargs: {"text": '{"messages":["断掉', "tokens": 41, "provider": "mock"}
    rid = accept(chat)["batch"]["id"]
    generate(chat, rid)
    assert engine.updates(room)["batch"]["status"] == "FAILED"
    assert len(service.messages(room)) == 1
    assert service.store.all("chat_generation")[0]["tokens"] == 41


def test_shutdown_during_optional_followup_finishes_main_reply_without_retry(chat):
    service, engine, provider, room, clock = chat
    provider.parts, provider.followup = ["这件事挺有意思。"], "还有一点我想到了。"
    engine.presence({"conversation_id": room, "tab_id": "tab", "visible": True, "composing": False})
    rid = accept(chat)["batch"]["id"]
    generate(chat, rid)
    engine._tick(dispatch=False)
    assert engine.updates(room)["batch"]["status"] == "WAITING_FOLLOWUP"
    engine.close()
    assert engine.updates(room)["batch"]["status"] == "COMPLETED"
    assert service.messages(room)[0]["reply_state"] == "REPLIED"
    engine.retry(rid)
    engine._tick(dispatch=False)
    assert len(service.messages(room)) == 2


def test_legacy_completed_request_is_adopted_without_another_model_call(chat):
    service, engine, provider, room, clock = chat
    with service.store.transaction():
        user_id = service.store.add_message(room, "USER", "旧的完整请求", request_key="legacy",
                                            request_domain="general", remember_requested=False)
        agent_id = service.store.add_message(room, "AGENT", "旧回复保留原样。", request_key="legacy")
    result = service.send(room, "旧的完整请求", False, "general", "legacy")
    assert result["user"]["id"] == user_id
    assert result["agent"]["id"] == agent_id
    assert result["agent"]["text"] == "旧回复保留原样。"
    assert len(service.messages(room)) == 2
    assert provider.calls == []


def test_late_memory_error_does_not_resurrect_cancelled_batch_or_fail_new_turn(chat, monkeypatch):
    service, engine, provider, room, clock = chat
    started, release = threading.Event(), threading.Event()
    errors = []

    def delayed_admit(*args):
        started.set()
        assert release.wait(5)
        raise DomainError("记忆暂时未完成", 503)

    monkeypatch.setattr(service, "admit", delayed_admit)

    def submit():
        try:
            accept(chat, "这一条授权记住", "memory-first", True)
        except DomainError as exc:
            errors.append(exc)

    thread = threading.Thread(target=submit)
    thread.start()
    assert started.wait(5)
    original = engine.updates(room)["batch"]["id"]
    newer = accept(chat, "我还没说完", "memory-newer")
    release.set()
    thread.join(5)
    assert len(errors) == 1
    assert service.require(original, "reply_batch")["status"] == "CANCELLED"
    assert engine.updates(room)["batch"]["id"] == newer["batch"]["id"]
    assert engine.updates(room)["batch"]["status"] == "QUEUED"
    assert all(m["reply_state"] == "WAITING" for m in service.messages(room))


def test_partial_publication_retry_uses_saved_tail_without_duplicate_or_model(chat):
    service, engine, provider, room, clock = chat
    rid = accept(chat)["batch"]["id"]
    generate(chat, rid)
    engine._tick(dispatch=False)
    with engine.lock, service.store.transaction():
        batch = service.require(rid, "reply_batch")
        batch.update(status="FAILED", failure="发布暂时中断")
        engine._save(batch)
    engine.retry(rid)
    engine.part_interval = (0, 0)
    engine._tick(dispatch=False)
    engine._tick(dispatch=False)
    assert len(service.messages(room)) == 4
    assert [m["text"] for m in service.messages(room)[1:]] == provider.parts
    assert len(provider.calls) == 1


def test_async_api_requires_key_and_never_exposes_queued_parts(tmp_path):
    provider = BurstProvider()
    with TestClient(create_app(tmp_path / "api", provider), base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        room = client.post("/api/v1/real-conversations", json={"skill_id": "skill_buffett"}).json()["id"]
        path = f"/api/v1/real-conversations/{room}"
        assert client.post(path + "/turns", json={"text": "你好"}).status_code == 400
        response = client.post(path + "/turns", json={"text": "你好"}, headers={"Idempotency-Key": "async"})
        assert response.status_code == 202
        rid = response.json()["batch"]["id"]
        replay = client.post(path + "/turns", json={"text": "你好"}, headers={"Idempotency-Key": "async"})
        assert replay.json()["batch"]["id"] == rid
        update = client.get(path + "/updates").json()
        assert "parts" not in update["batch"]
        assert "idle_followup" not in update["batch"]
        assert len(update["messages"]) == 1
        assert client.put("/api/v1/chat-presence", json={"conversation_id": room, "tab_id": "test", "visible": True, "composing": False}).status_code == 200
        assert client.put("/api/v1/chat-presence", json={"conversation_id": room, "tab_id": "test", "visible": True, "composing": False, "draft": "不允许上传"}).status_code == 422
