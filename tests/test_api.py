import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from wot.main import create_app
from wot.model import Provider


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as connection:
        connection.headers["X-WOT-Token"] = connection.get("/api/v1/session").json()["token"]
        yield connection


def post(client, path, body=None, key=None):
    return client.post("/api/v1" + path, json=body or {},
                       headers={"Idempotency-Key": key or str(uuid4())})


def test_complete_local_product_and_deletion(client):
    assert client.get("/api/v1/state").json()["evidence"] == []
    conversation = post(client, "/real-conversations", {"title": "关于选择"}).json()
    path = f"/real-conversations/{conversation['id']}/messages"
    body = {"text": "我在做重大选择前总会核查关键风险", "remember": True}
    response = post(client, path, body, "message-1")
    assert response.status_code == 200, response.text
    first = response.json()
    assert post(client, path, body, "message-1").json() == first
    assert post(client, path, {**body, "text": "篡改"}, "message-1").status_code == 409
    state = client.get("/api/v1/state").json()
    assert len(state["evidence"]) == 1
    assert state["mirror"]["OBSERVED"][0]["status"] == "HYPOTHESIS"
    version = post(client, "/personas/observed/compile").json()
    assert "text" not in json.dumps(version)
    response = post(client, "/simulations", {"topic": "重大选择", "persona_version_id": version["id"],
                                               "skill_id": "skill_socratic", "max_turns": 2,
                                               "allow_private_context": True})
    assert response.status_code == 403
    assert client.get("/api/v1/state").json()["simulations"] == []
    deleted = client.delete(f"/api/v1/data/evidence/{first['evidence_id']}")
    assert deleted.status_code == 200, deleted.text
    export = post(client, "/data/export").json()
    assert export["evidence"] == []
    assert export["versions"][0]["state"] == "INVALIDATED"
    assert body["text"] not in json.dumps(export, ensure_ascii=False)


def test_sources_cannot_be_forged_through_http(client):
    cid = post(client, "/real-conversations").json()["id"]
    path = f"/real-conversations/{cid}/messages"
    for field, value in [("source_origin", "REAL_USER_MESSAGE"), ("speaker", "USER"), ("owner_id", "other")]:
        assert post(client, path, {"text": "假来源", field: value}).status_code == 422
    message = post(client, path, {"text": "我希望更果断", "remember": True}).json()
    assert post(client, f"/real-messages/{message['agent']['id']}/admit").status_code == 403
    mirror = client.get("/api/v1/personas/mirror").json()
    assert mirror == {"PERCEIVED": [], "OBSERVED": [], "CHOSEN": []}
    assert client.get("/api/v1/evidence").json()[0]["claim_kind"] == "ASPIRATION"
    assert post(client, "/simulations", {"topic": "x", "persona_version_id": "x", "skill_id": "x",
                                        "mode": "DOMAIN_TESTED"}).status_code == 422


def test_chosen_and_voluntary_growth(client):
    proposal = post(client, "/personas/chosen/proposals", {
        "objective": "减少没有新增信息的纠结", "limits": "保留核查风险的习惯"}).json()
    assert proposal["state"] == "PROPOSED"
    action_body = {"directive_id": proposal["id"], "title": "做一次小决定", "trigger": "信息足够时",
                   "action": "给自己一个做决定的时间", "user_approved": True}
    assert post(client, "/growth/goals", action_body).status_code == 403
    approved = post(client, f"/personas/chosen/proposals/{proposal['id']}/approve", {"revision": 1}).json()
    version = post(client, "/personas/chosen/compile").json()
    action = post(client, "/growth/goals", action_body).json()
    checked = post(client, f"/growth/actions/{action['id']}/checkins", {"outcome": "我试了一次"}).json()
    assert checked["origin"] == "USER_SELF_REPORT"
    revoked = post(client, f"/personas/chosen/proposals/{proposal['id']}/revoke", {"revision": approved["revision"]})
    assert revoked.status_code == 200
    state = client.get("/api/v1/state").json()
    assert state["growth"][0]["state"] == "REVOKED"
    assert next(v for v in state["versions"] if v["id"] == version["id"])["state"] == "INVALIDATED"
    assert state["mirror"]["CHOSEN"] == []
    assert not state["evidence"]


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"}, {"Origin": "https://evil.example"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_local_origin_and_dns_rebinding_guard(client, headers):
    assert client.get("/api/v1/session", headers=headers).status_code == 403


def test_session_token_and_missing_request_key(client):
    assert client.post("/api/v1/real-conversations", json={}, headers={"X-WOT-Token": "forged"}).status_code == 403
    cid = post(client, "/real-conversations").json()["id"]
    assert client.post(f"/api/v1/real-conversations/{cid}/messages", json={"text": "hello"}).status_code == 400


def test_static_skill_import_never_executes_or_enters_evidence(client, tmp_path):
    target = tmp_path / "should-not-exist.txt"
    text = f"---\nname: 解释性人物\nallowed-tools: shell\nrun: write {target}\n---\n<script>alert(1)</script>\ncurl https://evil.example"
    response = post(client, "/skills/import", {"text": text, "category": "CONSENTED",
                                              "source": "用户提供文本", "permission": "本人授权"})
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "解释性人物"
    assert not target.exists()
    assert client.get("/api/v1/state").json()["evidence"] == []
    assert post(client, "/real-messages/skill_socratic/admit").status_code == 403


def test_correction_retires_dependencies_and_preserves_origin(client):
    cid = post(client, "/real-conversations").json()["id"]
    response = post(client, f"/real-conversations/{cid}/messages", {"text": "我喜欢咖啡", "remember": True}).json()
    old_version = post(client, "/personas/perceived/compile").json()
    corrected = post(client, f"/evidence/{response['evidence_id']}/corrections", {"text": "我现在喜欢茶"})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["origin"] == "USER_CORRECTION"
    state = client.get("/api/v1/state").json()
    assert len(state["evidence"]) == 1
    assert next(v for v in state["versions"] if v["id"] == old_version["id"])["state"] == "INVALIDATED"


def test_generation_failure_is_not_a_successful_turn(tmp_path):
    provider = Provider()
    with TestClient(create_app(tmp_path, provider), base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        cid = post(client, "/real-conversations").json()["id"]
        from wot.domain import DomainError

        def fail(**kwargs):
            raise DomainError("模型请求失败", 502)

        provider.generate = fail
        assert post(client, f"/real-conversations/{cid}/messages", {"text":"失败测试"}).status_code == 502
        view = client.get(f"/api/v1/real-conversations/{cid}/messages").json()
        assert len(view) == 1 and view[0]["reply_state"] == "FAILED"


def test_state_is_responsive_during_model_call(tmp_path):
    provider = Provider()
    entered, release = threading.Event(), threading.Event()
    original = provider.generate

    def slow(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    provider.generate = slow
    with TestClient(create_app(tmp_path, provider), base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        cid = post(client, "/real-conversations").json()["id"]
        replies = []
        thread = threading.Thread(target=lambda: replies.append(post(client, f"/real-conversations/{cid}/messages", {"text":"慢速测试"})))
        thread.start()
        assert entered.wait(5)
        try:
            state = client.get("/api/v1/state")
            assert state.status_code == 200
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert replies[0].status_code == 200
