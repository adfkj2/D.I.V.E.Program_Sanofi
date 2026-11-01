"""Published source lineage, local routes and quota integration; no real services."""

import json

import httpx
import pytest
from test_upgrade_api import open_client
from test_virtual_upgrade import create, generate, publish
from test_virtual_upgrade import rig as rig
from wot.domain import DomainError
from wot.main import create_app
from wot.research import ResearchManager
from wot.service import WotService

SECRET = "tvly-isolated-integration-credential"
URL = "https://www.example.edu.cn/admissions/2026"


def transport(request):
    assert request.url.host == "api.tavily.com"
    assert request.headers["authorization"] == "Bearer " + SECRET
    if request.url.path == "/search":
        return httpx.Response(200, json={"results": [{"url": URL, "title": "2026年招生说明"}],
                                         "usage": {"credits": 1}})
    return httpx.Response(200, json={"results": [{"url": URL, "raw_content":
        "2026年山东省普通类本科批。物理、化学。往年参考最低分580分，位次30000名；不保证录取。"}],
        "usage": {"credits": 2}})


def install(service, directory):
    client = httpx.Client(transport=httpx.MockTransport(transport))
    manager = ResearchManager(service, directory, http_client=client,
        resolver=lambda *a, **kw: [(2, 1, 6, "", ("8.8.8.8", 443))])
    service.research = manager
    service.invalidate_hooks.append(manager.invalidate)
    assert manager.validate({"api_key": SECRET, "remember_key": False})["saved"]
    return manager, client


def test_web_routes_are_read_only_until_explicit_test_and_preserve_failed_form_config(tmp_path):
    app = create_app(tmp_path)
    manager = app.state.upgrade.research
    manager.client.close()
    manager.client = httpx.Client(transport=httpx.MockTransport(transport))
    with open_client(app) as client:
        assert not client.get("/api/v1/web/config").json()["configured"]
        assert manager.store.all("research_call") == []
        result = client.post("/api/v1/web/config/validate", json={
            "api_key": SECRET, "remember_key": False, "enabled": True}).json()
        assert result["status"] == "ADDED" and result["saved"]
        revision = result["config"]["revision"]
        manager.client.close()
        manager.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text=SECRET)))
        failed = client.post("/api/v1/web/config/validate", json={
            "api_key": "tvly-bad-test-credential", "remember_key": False}).json()
        assert failed["status"] == "TEST_FAILED" and not failed["saved"]
        assert failed["config"]["revision"] == revision
        assert failed["config"]["configured"]
        for response in (client.get("/api/v1/state"), client.post("/api/v1/data/export", json={})):
            assert SECRET not in response.text
        assert SECRET not in (tmp_path / "wot.db").read_bytes().decode("utf-8", "ignore")
        assert client.delete("/api/v1/web/config").json()["configured"] is False


def test_sources_only_publish_with_actual_chat_bubble_and_delete_with_evidence(tmp_path):
    service = WotService(tmp_path)
    manager, client = install(service, tmp_path)
    try:
        room = service.conversation("志愿", "skill_zhangxuefeng")["id"]
        receipt = service.chat.accept(room, "我在山东，想了解报考", True, "career", "web-first")
        batch = service.require(receipt["batch"]["id"], "reply_batch")
        aid = service.agents.ensure(scope_kind="CHAT", scope_id=room,
            participant_id=batch["skill_id"], version_id=batch["skill_version"])
        run = manager.begin(scope_kind="CHAT", scope_id=room, agent_id=aid,
            generation_id="test-generation", version_id=batch["skill_version"],
            source_message_ids=batch["source_message_ids"])
        evidence = manager.collect(run, ["2026 山东 本科 物理 化学 招生"])
        assert evidence["sources"]
        source_text = evidence["sources"][0]["excerpt"]
        with pytest.raises(DomainError):
            manager.sources(run)
        with service.store.transaction():
            batch.update(status="DELIVERING", generated=True, parts=["先看同省同选科的位次。"],
                next_index=0, idle_followup=None, research_id=run, agent_id=aid)
            service.store.update(batch)
            service.chat._publish(batch)
        message = service.messages(room)[-1]
        assert message["research_id"] == run and message["source_refs"]
        assert manager.sources(run)["sources"][0]["url"] == URL
        count = len(service.store.all("research_call"))
        manager.sources(run)
        assert len(service.store.all("research_call")) == count
        service.delete(receipt["user"]["evidence_id"])
        with pytest.raises(DomainError):
            manager.sources(run)
        assert not service.messages(room)[-1]["source_refs"]
        assert source_text not in json.dumps(service.export(), ensure_ascii=False)
    finally:
        client.close()
        service.close()


def test_chat_switch_cancels_pending_not_history_and_does_not_call_network(tmp_path):
    service = WotService(tmp_path)
    manager, client = install(service, tmp_path)
    try:
        room = service.conversation("日常", "skill_munger")["id"]
        receipt = service.chat.accept(room, "今天聊什么", False, "general", "toggle")
        calls = len(manager.store.all("research_call"))
        assert not service.chat.web(room, False)["web_enabled"]
        assert service.chat.public(service.require(receipt["batch"]["id"], "reply_batch"))["status"] == "CANCELLED"
        assert len(manager.store.all("research_call")) == calls
        assert service.chat.web(room, True)["web_enabled"]
    finally:
        client.close()
        service.close()


def test_virtual_search_reservation_is_idempotent_and_limit_is_resumable(rig):
    rid = create(rig, start_immediately=True, max_web_requests=1)["id"]
    original = rig.service.agents.respond

    def reserve_and_respond(**kwargs):
        kwargs["on_web_request"]("request-one")
        kwargs["on_web_request"]("request-one")
        kwargs["on_web_request"]("request-two")
        return original(**kwargs)

    rig.service.agents.respond = reserve_and_respond
    paused = generate(rig, rid)
    assert paused["state"] == "PAUSED_LIMIT" and paused["pause_reason"] == "WEB_REQUEST_LIMIT"
    assert paused["used_web_requests"] == 1 and not rig.connections.calls
    with pytest.raises(DomainError, match="联网"):
        rig.virtual.control(rid, "resume")
    body = {"add_web_requests": 20}
    for _ in range(2):
        rig.service.once("web-topup", body, lambda: rig.virtual.top_up(rid, body))
    assert rig.virtual.view(rid)["max_web_requests"] == 21
    assert not rig.connections.calls
    rig.service.agents.respond = original
    assert rig.virtual.control(rid, "resume")["state"] == "RUNNING"
    assert generate(rig, rid)["draft"]
    assert len(rig.connections.calls) == 1


def test_key_removal_pauses_pending_virtual_turn_without_revoking_participants(rig, tmp_path):
    manager, client = install(rig.service, tmp_path)
    try:
        rid = create(rig, start_immediately=True)["id"]
        room = generate(rig, rid)
        with rig.service.store.transaction():
            current = rig.virtual._session(rid)
            draft = current["draft"]
            run = manager.begin(scope_kind="VIRTUAL", scope_id=rid, agent_id=draft["agent_id"],
                generation_id=draft["generation_id"], version_id=draft["version_id"])
            draft["research_id"] = run
            rig.service.store.update(current)
        agents = dict(room["agent_ids"]) if "agent_ids" in room else dict(rig.virtual._session(rid)["agent_ids"])
        manager.remove()
        paused = publish(rig, rid)
        assert paused["state"] == "PAUSED" and not paused["turns"]
        assert not rig.virtual._session(rid)["draft"]
        assert not rig.virtual._session(rid)["burst_pending"]
        assert rig.virtual.control(rid, "resume")["state"] == "RUNNING"
        assert generate(rig, rid)["draft"]
        assert rig.virtual._session(rid)["agent_ids"] == agents
    finally:
        client.close()
