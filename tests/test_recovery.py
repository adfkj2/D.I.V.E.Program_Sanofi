"""Crash recovery, deletion replay and loopback boundary regression tests."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from wot.domain import DomainError
from wot.main import create_app
from wot.model import Provider
from wot.service import WotService


@pytest.mark.parametrize("kind", ["conversation", "directive", "growth"])
def test_action_commit_before_receipt_failure_recovers_once_after_restart(tmp_path, kind):
    service = WotService(tmp_path)
    directive_id = None
    if kind == "growth":
        proposal = service.propose("先核查再选择", "career", "保留退出空间")
        service.directive(proposal["id"], 1, "approve")
        directive_id = proposal["id"]

    def action(target):
        if kind == "conversation":
            return target.conversation("同一次会话", None)
        if kind == "directive":
            return target.propose("同一个选择", "career", "不替代本人决定")
        return target.growth(directive_id, "一次尝试", "信息足够时", "写下一项选择", True)

    # The operation itself commits successfully. Only its final receipt update
    # fails, exercising the real inter-transaction crash window.
    service.store.db.execute(
        "CREATE TRIGGER fail_receipt BEFORE UPDATE OF response ON requests "
        "WHEN NEW.response <> '' BEGIN SELECT RAISE(FAIL,'injected receipt failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        service.once("crash-key", {"path": kind, "text": "original"}, lambda: action(service))
    existing = service.store.all(kind)
    assert len(existing) == 1
    expected_id = existing[0]["id"]
    receipt = service.store.db.execute("SELECT * FROM requests WHERE key='crash-key'").fetchone()
    assert receipt is not None and receipt["response"] == ""
    service.store.db.execute("DROP TRIGGER fail_receipt")
    service.store.db.commit()
    service.close()

    recovered = WotService(tmp_path)
    try:
        with pytest.raises(DomainError) as conflict:
            recovered.once("crash-key", {"path": kind, "text": "changed"}, lambda: action(recovered))
        assert conflict.value.status == 409
        result = recovered.once("crash-key", {"path": kind, "text": "original"}, lambda: action(recovered))
        assert result["id"] == expected_id
        assert len(recovered.store.all(kind)) == 1
        assert recovered.once("crash-key", {"path": kind, "text": "original"}, lambda: pytest.fail("must use receipt"))["id"] == expected_id
    finally:
        recovered.close()


def test_correction_admission_failure_retries_same_operation_after_restart(tmp_path, monkeypatch):
    service = WotService(tmp_path)
    conversation = service.conversation("更正恢复", None)
    sent = service.send(conversation["id"], "我会先保持原来的选择。", True, "career", "original-message")
    evidence_id = sent["evidence_id"]
    original_ingest = service.core.ingest

    def fail_correction(namespace, *args, **kwargs):
        if namespace.endswith("/corrections"):
            raise RuntimeError("injected new correction admission failure")
        return original_ingest(namespace, *args, **kwargs)

    monkeypatch.setattr(service.core, "ingest", fail_correction)
    body = {"path": "correction/" + evidence_id, "text": "我现在会先核查再选择。", "domain": "career"}
    with pytest.raises(DomainError) as failure:
        service.once("correction-operation", body, lambda: service.correct(evidence_id, body["text"], "career"))
    assert failure.value.status == 503
    assert service.evidence(evidence_id)["status"] == "DELETED"
    assert len(service.store.all("correction")) == 1
    service.close()

    recovered = WotService(tmp_path)
    try:
        result = recovered.once("correction-operation", body, lambda: recovered.correct(evidence_id, body["text"], "career"))
        assert result["origin"] == "USER_CORRECTION"
        assert result["status"] == "ACTIVE"
        active = recovered.active_evidence()
        assert len(active) == 1 and active[0]["id"] == result["id"]
        assert len(recovered.store.all("correction")) == 1
        messages = recovered.messages(conversation["id"])
        assert sum(message["text"] == body["text"] for message in messages) == 1
    finally:
        recovered.close()


def test_deletion_purges_cached_private_bodies_without_losing_replay_fingerprints(tmp_path):
    service = WotService(tmp_path)
    canary = "原始隐私_CANARY_382761"
    conversation = service.conversation("删除恢复", None)
    body = {"path": "send/" + conversation["id"], "text": "我会先核查风险，" + canary}
    sent = service.once("send-once", body, lambda: service.send(conversation["id"], body["text"], True, "career", "send-once"))
    version = service.once("compile-once", {"path": "compile/OBSERVED"}, lambda: service.compile("OBSERVED"))
    assert canary in json.dumps(service.provider.calls, ensure_ascii=False)
    assert canary in service.store.db.execute("SELECT response FROM requests WHERE key='send-once'").fetchone()[0]
    fingerprint = service.store.db.execute("SELECT fingerprint FROM requests WHERE key='send-once'").fetchone()[0]
    service.once("delete-once", {"path": "delete/" + sent["evidence_id"]}, lambda: service.delete(sent["evidence_id"]))
    receipt = service.store.db.execute("SELECT * FROM requests WHERE key='send-once'").fetchone()
    assert receipt is not None and receipt["response"] == "" and receipt["fingerprint"] == fingerprint
    assert service.provider.calls == []
    assert canary not in json.dumps(service.export(), ensure_ascii=False)
    assert service.require(version["id"], "version")["state"] == "INVALIDATED"
    with pytest.raises(DomainError) as conflict:
        service.once("send-once", {**body, "text": "different"}, lambda: pytest.fail("fingerprint must reject reuse"))
    assert conflict.value.status == 409
    service.close()

    recovered = WotService(tmp_path)
    try:
        assert recovered.active_evidence() == []
        assert recovered.core.retrieve("user/local/real", "CANARY") == []
        assert canary not in json.dumps(recovered.export(), ensure_ascii=False)
        assert recovered.require(version["id"], "version")["state"] == "INVALIDATED"
        with pytest.raises(DomainError):
            recovered.once("send-once", body, lambda: recovered.send(conversation["id"], body["text"], True, "career", "send-once"))
        rebuilt = recovered.once("compile-once", {"path": "compile/OBSERVED"}, lambda: recovered.compile("OBSERVED"))
        assert rebuilt["id"] != version["id"] and rebuilt["evidence_ids"] == []
    finally:
        recovered.close()


@pytest.mark.parametrize("peer", ["203.0.113.1", "192.168.1.50", "2001:db8::10"])
def test_remote_peer_cannot_bypass_local_guard_with_forged_host(tmp_path, peer):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8765", client=(peer, 51234)) as client:
        for path in ("/api/v1/session", "/api/v1/state", "/api/v1/healthz"):
            response = client.get(path, headers={"Host": "localhost:8765", "Origin": "http://localhost:8765"})
            assert response.status_code == 403
            assert "token" not in response.json()


@pytest.mark.parametrize("peer,host", [("127.0.0.1", "localhost:8765"), ("::1", "[::1]:8765")])
def test_actual_loopback_peers_can_bootstrap_but_mutation_still_requires_token(tmp_path, peer, host):
    app = create_app(tmp_path)
    # The installed TestClient cannot parse an IPv6 base_url; an IPv6 peer and
    # Host header still exercise the actual middleware without that client bug.
    with TestClient(app, base_url="http://localhost:8765", client=(peer, 51234)) as client:
        client.headers["Host"] = host
        session = client.get("/api/v1/session")
        assert session.status_code == 200
        denied = client.post("/api/v1/real-conversations", json={"title": "x"})
        assert denied.status_code == 403
        accepted = client.post("/api/v1/real-conversations", json={"title": "x"}, headers={"x-wot-token": session.json()["token"]})
        assert accepted.status_code == 200


def test_private_excerpt_does_not_reach_external_role_without_separate_public_approval(tmp_path):
    canary = "CANARY-761"

    class ExcerptProvider(Provider):
        def generate(self, **kwargs):
            if kwargs["role"] == "PERSONA":
                assert kwargs["private"]
                self.calls.append(kwargs)
                return {"text": f"AI 模拟：私密编号是 {canary}。", "tokens": 20, "cost": 0, "provider": "mock"}
            return super().generate(**kwargs)

    provider = ExcerptProvider()
    service = WotService(tmp_path, provider)
    try:
        conversation = service.conversation("公开审批", None)
        service.send(conversation["id"], f"我会核查风险，同时我的私密编号是 {canary}，请勿公开。", True, "general", "source")
        version = service.compile("OBSERVED")
        session = service.simulation("核查风险", version["id"], "skill_socratic", 4, True)
        provider.calls.clear()
        local_preview = service.next_turn(session["id"], "persona-turn")
        assert canary in local_preview["turns"][0]["text"]
        assert len(provider.calls) == 1
        with pytest.raises(DomainError) as denied:
            service.next_turn(session["id"], "external-turn")
        assert denied.value.status == 403
        assert len(provider.calls) == 1  # no hidden external call before approval
        approved = service.next_turn(session["id"], "external-turn-approved", disclosure_approved=True)
        assert len(approved["turns"]) == 2
        external = provider.calls[-1]
        assert external["role"] == "EXTERNAL"
        assert external["private"] == [] and external["goals"] == []
        assert any(canary in turn["text"] for turn in external["transcript"])
    finally:
        service.close()
