"""IM persistence, migration and role boundaries exercised against real SQLite."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from wot.catalog import builtin_people, role_context
from wot.domain import DomainError
from wot.main import create_app
from wot.model import Provider
from wot.service import WotService
from wot.storage import SCHEMA, AppStore, canonical


@pytest.fixture
def app_service(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    monkeypatch.setenv("WOT_MODEL", "mock-reflection-v1")
    service = WotService(tmp_path / "data")
    yield service
    assert service.provider.external_calls == 0
    service.close()


def legacy_database(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    stamp = "2026-10-30T10:00:00+00:00"
    skill = {"id": "skill_custom", "created_at": stamp, "display_name": "原来的角色",
             "text": "用户自行写下的角色资料", "status": "ACTIVE", "version": 7,
             "category": "PRIVATE", "permission": "用户提供", "source": "本地"}
    conversation = {"id": "old_conversation", "created_at": stamp,
                    "title": "原来的聊天", "skill_id": "skill_custom"}
    for kind, data in (("skill", skill), ("conversation", conversation)):
        db.execute("INSERT INTO resources VALUES(?,?,?,?,?)",
                   (data["id"], kind, "local", canonical(data), stamp))
    for mid, speaker, text, key in (("z_first", "USER", "第一条消息", "old-1"),
                                     ("a_second", "AGENT", "原来的回答", "old-1"),
                                     ("m_third", "USER", "旧版未得到回答的消息", "old-2")):
        db.execute("INSERT INTO messages(id,conversation_id,owner_id,speaker,text,created_at,"
                   "evidence_id,request_key) VALUES(?,?,'local',?,?,?,NULL,?)",
                   (mid, conversation["id"], speaker, text, stamp, key))
    db.commit()
    db.close()


def test_v1_migration_creates_readable_prechange_backup_and_stable_row_order(tmp_path):
    path = tmp_path / "wot.db"
    legacy_database(path)
    store = AppStore(path)
    backup_path = store.migration_backup_path
    assert backup_path is not None and backup_path.is_file()
    assert store.db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    rows = store.db.execute("SELECT id,sequence,reply_state,client_request_id FROM messages "
                            "ORDER BY sequence").fetchall()
    assert [r["id"] for r in rows] == ["z_first", "a_second", "m_third"]
    assert [r["sequence"] for r in rows] == [1, 2, 3]
    assert rows[0]["reply_state"] == "REPLIED" and rows[2]["reply_state"] == "FAILED"
    assert rows[0]["client_request_id"] == "old-1"
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
        assert "sequence" not in {r[1] for r in backup.execute("PRAGMA table_info(messages)")}
        assert backup.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    finally:
        backup.close()
        store.db.close()
    reopened = AppStore(path)
    try:
        assert reopened.migration_backup_path is None
        assert len(list(tmp_path.glob("wot.pre-v3.*.db"))) == 1
    finally:
        reopened.db.close()


def test_existing_user_gets_each_missing_builtin_without_losing_custom_role_or_chats(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    directory = tmp_path / "data"
    directory.mkdir()
    legacy_database(directory / "wot.db")
    store = AppStore(directory / "wot.db")
    with store.transaction():
        store.add("skill", {"display_name": "我修改过的芒格", "text": "保留我的本地修改",
                            "status": "ACTIVE", "version": 77}, rid="skill_munger")
    store.db.close()
    service = WotService(directory)
    try:
        assert {p["id"] for p in builtin_people()} <= {p["id"] for p in service.store.all("skill")}
        assert len(service.store.all("skill")) == 17
        assert service.require("skill_custom", "skill")["version"] == 7
        assert service.require("skill_munger", "skill")["text"] == "保留我的本地修改"
        assert service.require("old_conversation", "conversation")["skill_id"] == "skill_custom"
        assert [m["id"] for m in service.messages("old_conversation")] == ["z_first", "a_second", "m_third"]
        assert service.messages("old_conversation")[-1]["retry_payload"] is None
    finally:
        service.close()
    reopened = WotService(directory)
    try:
        assert len(reopened.store.all("skill")) == 17
        assert reopened.require("skill_munger", "skill")["version"] == 77
    finally:
        reopened.close()


def test_legacy_later_admission_does_not_fabricate_original_send_parameters(tmp_path):
    path = tmp_path / "wot.db"
    legacy_database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO evidence(id,owner_id,subject_id,message_id,origin,claim_kind,domain,text,"
            "namespace,stable_key,status,created_at,occurred_at,consent_scope,visibility_scope,content_hash) "
            "VALUES('old-evidence','local','local','m_third','REAL_USER_MESSAGE','OBSERVATION',"
            "'later-admission-domain','旧版未得到回答的消息','user/local/real','old-key','ACTIVE',"
            "'2026-10-30T10:00:00+00:00','2026-10-30T10:00:00+00:00','MIRROR','PRIVATE','old-hash')")
        db.execute("UPDATE messages SET evidence_id='old-evidence' WHERE id='m_third'")
    store = AppStore(path)
    try:
        old = store.db.execute("SELECT * FROM messages WHERE id='m_third'").fetchone()
        assert old["reply_state"] == "FAILED"
        assert old["request_domain"] is None and old["remember_requested"] is None
    finally:
        store.db.close()


def test_provider_failure_persists_original_retry_payload_and_same_key_reuses_user(app_service, monkeypatch):
    conversation = app_service.conversation("风险与选择", "skill_taleb")
    original = app_service.provider.generate

    def fail(**kwargs):
        raise DomainError("离线测试中的一次失败", 502)

    monkeypatch.setattr(app_service.provider, "generate", fail)
    body = {"text": "我要如何看待风险？", "remember": True, "domain": "career"}
    with pytest.raises(DomainError):
        app_service.send(conversation["id"], **body, key="client-stable-1")
    failed = app_service.messages(conversation["id"])
    assert len(failed) == 1 and failed[0]["reply_state"] == "FAILED"
    assert failed[0]["client_request_id"] == "client-stable-1"
    assert failed[0]["sequence"] == 1
    assert failed[0]["retry_payload"] == body
    monkeypatch.setattr(app_service.provider, "generate", original)
    result = app_service.send(conversation["id"], **failed[0]["retry_payload"],
                              key=failed[0]["client_request_id"])
    assert result["user"]["id"] == failed[0]["id"]
    assert result["user"]["reply_state"] == "REPLIED"
    assert result["user"]["retry_payload"] is None
    assert result["agent"]["sequence"] == 2
    assert len(app_service.active_evidence()) == 1
    assert len(app_service.messages(conversation["id"])) == 2
    assert app_service.send(conversation["id"], **body, key="client-stable-1") == result


def test_refresh_after_failed_api_send_reconstructs_exact_body_and_preserves_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    provider = Provider()
    original = provider.generate
    monkeypatch.setattr(provider, "generate", lambda **k: (_ for _ in ()).throw(DomainError("失败", 502)))
    app = create_app(tmp_path / "data", provider)
    with TestClient(app, base_url="http://localhost:8765") as client:
        token = client.get("/api/v1/session").json()["token"]
        headers = {"x-wot-token": token, "idempotency-key": "conversation-create"}
        conversation = client.post("/api/v1/real-conversations", headers=headers,
                                   json={"title": "方法", "skill_id": "skill_munger"}).json()
        headers["idempotency-key"] = "message-original-body"
        path = f"/api/v1/real-conversations/{conversation['id']}/messages"
        body = {"text": "先看看什么会失败。", "remember": True, "domain": "decisions"}
        assert client.post(path, headers=headers, json=body).status_code == 502
    restarted_provider = Provider()
    monkeypatch.setattr(restarted_provider, "generate", original)
    restarted = create_app(tmp_path / "data", restarted_provider)
    with TestClient(restarted, base_url="http://localhost:8765") as client:
        token = client.get("/api/v1/session").json()["token"]
        message = client.get(path).json()[0]
        assert message["reply_state"] == "FAILED" and message["retry_payload"] == body
        headers = {"x-wot-token": token, "idempotency-key": message["client_request_id"]}
        response = client.post(path, headers=headers, json=message["retry_payload"])
        assert response.status_code == 200
        assert response.json()["user"]["id"] == message["id"]
        assert len(client.get(path).json()) == 2
        assert client.post(path, headers=headers, json={**body, "domain": "different"}).status_code == 409


def test_sequence_and_recent_conversations_follow_activity_even_with_identical_clocks(app_service, monkeypatch):
    monkeypatch.setattr("wot.storage.now", lambda: "2026-10-30T10:00:00+00:00")
    first = app_service.conversation("先创建", "skill_buffett")
    second = app_service.conversation("后创建", "skill_feynman")
    app_service.send(second["id"], "解释概念", False, "general", "second-first")
    app_service.send(first["id"], "长期价值", False, "general", "first-last")
    summaries = app_service.conversations()
    assert [c["id"] for c in summaries] == [first["id"], second["id"]]
    assert summaries[0]["updated_at"] == summaries[0]["last_message_at"]
    messages = app_service.messages(first["id"])
    assert [m["sequence"] for m in messages] == [1, 2]
    assert [m["speaker"] for m in messages] == ["USER", "AGENT"]
    with pytest.raises(sqlite3.IntegrityError), app_service.store.transaction():
        app_service.store.db.execute("UPDATE messages SET sequence=1 WHERE id=?", (messages[1]["id"],))


def test_deleted_messages_cannot_retry_or_leak_into_conversation_preview(app_service):
    conversation = app_service.conversation("私人决定", "skill_naval")
    original = app_service.send(conversation["id"], "我对机会做过一个私人的决定。", True,
                                "career", "private-source")
    app_service.delete(original["evidence_id"])
    messages = app_service.messages(conversation["id"])
    assert all(m["deleted"] for m in messages)
    assert all(m["retry_payload"] is None for m in messages)
    summary = app_service.conversations()[0]
    assert summary["last_message_preview"] == "[关联来源已删除]"
    assert original["user"]["text"] not in json.dumps(app_service.state(), ensure_ascii=False)
    stored_conversation = app_service.require(conversation["id"], "conversation")
    assert "last_message_preview" not in stored_conversation
    with pytest.raises(DomainError) as error:
        app_service.send(conversation["id"], original["user"]["text"], True,
                         "career", "private-source")
    assert error.value.status == 409


def test_corrections_get_unique_sequence_and_deleted_sources_keep_original_sequence(app_service):
    conversation = app_service.conversation("更正", "skill_drucker")
    first = app_service.send(conversation["id"], "昨天我接受了这个机会。", True,
                            "career", "initial")
    corrected = app_service.correct(first["evidence_id"], "昨天我拒绝了这个机会。", "career")
    messages = app_service.messages(conversation["id"])
    assert [m["sequence"] for m in messages] == [1, 2, 3]
    assert messages[0]["deleted"] and messages[1]["deleted"]
    assert messages[2]["evidence_id"] == corrected["id"]
    assert messages[2]["reply_state"] == "REPLIED"


def test_reply_replay_matches_original_request_even_after_a_later_message(app_service):
    conversation = app_service.conversation("多个请求", "skill_feynman")
    first = app_service.send(conversation["id"], "解释第一个概念", False, "learning", "first")
    app_service.send(conversation["id"], "解释第二个概念", False, "learning", "second")
    replay = app_service.send(conversation["id"], "解释第一个概念", False, "learning", "first")
    assert replay == first
    assert [m["sequence"] for m in app_service.messages(conversation["id"])] == [1, 2, 3, 4]
    with pytest.raises(DomainError):
        app_service.send(conversation["id"], "解释第一个概念", True, "learning", "first")
    with pytest.raises(DomainError):
        app_service.send(conversation["id"], "解释第一个概念", False, "different", "first")


def test_interrupted_replies_are_reconciled_without_startup_model_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    directory = tmp_path / "data"
    service = WotService(directory)
    conversation = service.conversation("中断恢复", None)
    with service.store.transaction():
        unanswered = service.store.add_message(
            conversation["id"], "USER", "尚未生成的回答", request_key="interrupted",
            request_domain="general", remember_requested=False, reply_state="WAITING")
        answered = service.store.add_message(
            conversation["id"], "USER", "已经生成但状态还没保存", request_key="committed",
            request_domain="general", remember_requested=False, reply_state="WAITING")
        service.store.add_message(conversation["id"], "AGENT", "已提交的回答", request_key="committed")
    service.close()
    recovered = WotService(directory)
    try:
        messages = {m["id"]: m for m in recovered.messages(conversation["id"])}
        assert messages[unanswered]["reply_state"] == "FAILED"
        assert messages[unanswered]["retry_payload"]["text"] == "尚未生成的回答"
        assert messages[answered]["reply_state"] == "REPLIED"
        assert messages[answered]["retry_payload"] is None
        assert recovered.provider.calls == [] and recovered.provider.external_calls == 0
    finally:
        recovered.close()


def test_deleted_flag_blocks_stale_body_at_read_and_preview_boundaries(app_service):
    conversation = app_service.conversation("删除标记边界", "skill_clear")
    with app_service.store.transaction():
        mid = app_service.store.add_message(
            conversation["id"], "USER", "STALE_PRIVATE_BODY", request_key="stale",
            request_domain="general", remember_requested=False, reply_state="FAILED")
        app_service.store.db.execute("UPDATE messages SET deleted=1 WHERE id=?", (mid,))
    message = app_service.messages(conversation["id"])[0]
    assert message["text"] == "[已删除]" and message["retry_payload"] is None
    assert app_service.conversations()[0]["last_message_preview"] == "[已删除]"
    assert "STALE_PRIVATE_BODY" not in json.dumps(app_service.state(), ensure_ascii=False)


def test_deleted_flag_blocks_admission_even_when_stale_original_body_remains(app_service):
    conversation = app_service.conversation("仅删除标记", None)
    sent = app_service.send(conversation["id"], "STALE_ADMISSION_CANARY", False,
                            "general", "not-remembered")
    with app_service.store.transaction():
        app_service.store.db.execute("UPDATE messages SET deleted=1 WHERE id=?", (sent["user"]["id"],))
    with pytest.raises(DomainError) as error:
        app_service.admit(sent["user"]["id"])
    assert error.value.status == 409
    assert app_service.active_evidence() == []
    assert app_service.core._service.store.all_events() == []


def test_delete_redacts_legacy_title_derived_from_first_message_and_cached_responses(app_service):
    text = "旧自动标题中的私密内容不应保留在左侧聊天列表里，这是超过二十四个字的原始发言。"
    prefix = text[:24]
    conversation = app_service.once("legacy-create", {"title": prefix},
                                    lambda: app_service.conversation(prefix, "skill_naval"))
    sent = app_service.once("legacy-send", {"text": text}, lambda: app_service.send(
        conversation["id"], text, True, "general", "legacy-send"))
    assert prefix in app_service.conversations()[0]["title"]
    app_service.delete(sent["evidence_id"])
    name = app_service.require("skill_naval", "skill")["display_name"]
    assert app_service.require(conversation["id"], "conversation")["title"] == f"与{name}的聊天"
    assert prefix not in json.dumps(app_service.state(), ensure_ascii=False)
    assert prefix not in json.dumps(app_service.export(), ensure_ascii=False)
    assert not any(prefix in row[0] for row in app_service.store.db.execute("SELECT response FROM requests"))


def test_builtin_mock_roles_are_distinct_and_explicitly_offline_while_custom_keeps_fallback(app_service):
    replies = []
    for person in builtin_people():
        reply = app_service.provider.generate(role="EXTERNAL", topic="选择一个机会", transcript=[],
                                               private=[], goals=[], skill=role_context(person))
        assert "离线演示" in reply["text"] and "非本人发言" in reply["text"]
        replies.append(reply["text"])
    assert len(replies) == len(set(replies)) == 15
    custom = app_service.provider.generate(role="EXTERNAL", topic="一个问题", transcript=[],
                                           private=[], goals=[], skill={"name": "自定义"})
    assert custom["text"] == "离线演示：如果先不急着回答，你最想厘清的是哪一个前提？"
    assert app_service.provider.external_calls == 0


def test_skill_state_and_details_omit_runtime_text_but_owner_export_retains_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://localhost:8765") as client:
        state = client.get("/api/v1/state").json()
        builtins = [p for p in state["skills"] if p.get("builtin")]
        assert len(builtins) == 15
        detail = client.get("/api/v1/skills/skill_munger").json()
        assert detail["source"] and detail["avatar_url"].startswith("/portraits/")
        assert "text" not in detail and "untrusted_role_data" not in detail
        assert all("text" not in person for person in state["skills"])
        token = client.get("/api/v1/session").json()["token"]
        export = client.post("/api/v1/data/export", headers={"x-wot-token": token}, json={}).json()
        assert next(p for p in export["skills"] if p["id"] == "skill_munger")["text"]
