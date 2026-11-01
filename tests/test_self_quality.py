"""SELF admission uses document quality, not strict-personal grants or mock scores."""

import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from wot.assessments import QUALITY_LABELS, QUALITY_PROTOCOL, AssessmentManager
from wot.domain import DomainError
from wot.main import create_app
from wot.people import PeopleManager
from wot.service import WotService
from wot.storage import canonical


class QualityConnections:
    def __init__(self):
        self.classification = "LIVE"
        self.scores = {key: 92 for key in QUALITY_LABELS}
        self.checks = {key: True for key in ("no_permission_escalation", "honest_identity", "source_declared")}
        self.calls = []
        self.fingerprint = "assessment-model-a"
        self.entered = self.release = None

    def active(self):
        return {"id": "test-connection", "provider": "test-only", "model": "test-only"}

    def is_live(self, connection_id=None):
        return True

    def config_hash(self, connection_id=None):
        return self.fingerprint

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        if self.entered:
            self.entered.set()
            assert self.release.wait(5)
        value = {"dimensions": {key: {"score": score, "issues": [], "suggestions": []}
                                for key, score in self.scores.items()}, "critical_checks": self.checks}
        return {"text": canonical(value), "tokens": 100, "classification": self.classification}


@pytest.fixture
def quality(tmp_path):
    service = WotService(tmp_path)
    people = PeopleManager(service, tmp_path)
    connections = QualityConnections()
    manager = AssessmentManager(service.store, tmp_path, connections, people.user_skill,
                                lambda rid: service.require(rid, "skill_import"))
    people.assessments = manager
    service.invalidate_hooks.append(people.invalidate)
    people.profile({"bio": "我是本人填写这份资料。我处理选择时先比较证据，表达习惯是先说具体的事再讨论理由。"})
    yield service, people, manager, connections
    manager.close()
    service.close()


def finish(manager, task):
    manager.workers[task["id"]].join(8)
    assert not manager.workers[task["id"]].is_alive()
    return manager.get(task["id"])


def assess(quality):
    _, people, manager, _ = quality
    version = people.user_skill()
    return version, finish(manager, manager.start_self(version["id"], confirmed=True))


def test_owner_quality_report_and_provenance_do_not_fabricate_public_license(quality):
    _, people, manager, connections = quality
    version, task = assess(quality)
    assert task["kind"] == "QUALITY" and task["subject_type"] == "USER_SKILL"
    assert task["status"] == "PASSED"
    assert task["report"]["title"] == "本人 Skill 资料质量评估"
    assert task["report"]["version"] == version["id"]
    assert task["success_message"] == "我的分身已可加入虚拟空间"
    document = connections.calls[0]["document"]
    assert document["assessment_source_manifest"][0]["type"] == "OWNER_BIO"
    assert document["content_hash"] == version["content_hash"]
    assert "不是公开授权或MIT许可" in document["permission"]
    assert manager.self_admission()["eligible"] is True
    assert people.me()["admitted"]["id"] == version["id"]
    assert people.me()["certified"] is None


def test_confirmation_and_meaningful_source_required_before_any_model(quality):
    _, people, manager, connections = quality
    version = people.user_skill()
    with pytest.raises(DomainError, match="确认"):
        manager.start_self(version["id"])
    people.profile({"bio": ""})
    empty = people.user_skill()
    with pytest.raises(DomainError, match="补充本人介绍"):
        manager.start_self(empty["id"], confirmed=True)
    assert connections.calls == []


@pytest.mark.parametrize("read", ["frozen", "unassessed_admission", "passed_admission"])
def test_read_guards_never_commit_outer_publication_transaction(quality, read):
    service, people, manager, _ = quality
    version = people.user_skill()
    if read == "passed_admission":
        version, _ = assess(quality)
    marker = "rollback-sentinel-" + read
    with pytest.raises(RuntimeError, match="rollback"):
        with service.store.transaction():
            service.store.add("test_sentinel", {"value": "not published"}, rid=marker)
            if read == "frozen":
                assert people.user_skill(version["id"])["id"] == version["id"]
            else:
                assert manager.self_admission(version["id"])["eligible"] is (read == "passed_admission")
            raise RuntimeError("rollback")
    assert service.store.get(marker, "test_sentinel") is None


@pytest.mark.parametrize("failure", ["mock", "single_dimension", "overall", "critical"])
def test_quality_thresholds_and_live_requirement_cannot_be_bypassed(quality, failure):
    _, _, manager, connections = quality
    if failure == "mock":
        connections.classification = "MOCK"
    elif failure == "single_dimension":
        connections.scores["consistency"] = 69
    elif failure == "overall":
        connections.scores = {key: 79 for key in QUALITY_LABELS}
    else:
        connections.checks["honest_identity"] = False
    version, task = assess(quality)
    assert task["status"] == "NOT_PASSED"
    assert manager.self_admission()["eligible"] is False
    with pytest.raises(DomainError):
        manager.assert_self_admitted(version["id"])


def test_admission_survives_model_change_cosmetics_and_new_draft(quality):
    _, people, manager, connections = quality
    version, task = assess(quality)
    connections.fingerprint = "entirely-different-model"
    people.profile({"nickname": "换个称呼", "bio": "更新的本人介绍，想继续积累更多真实资料。"})
    newer = people.user_skill()
    assert newer["id"] != version["id"]
    assert manager.assert_self_admitted(version["id"])["assessment_id"] == task["id"]
    assert manager.self_admission()["version_id"] == version["id"]
    assert manager.self_admission(newer["id"])["eligible"] is False
    assert len(connections.calls) == 1


def test_authorized_evidence_is_real_source_and_deletion_invalidates_exact_version(quality):
    service, people, manager, _ = quality
    evidence = people.statement("我喜欢先比较证据再做选择，表达时先说具体事情。")
    people.profile({"bio": ""})
    version, task = assess(quality)
    frozen = manager._private(task["id"])["snapshot"]
    assert frozen["evidence_ids"] == [evidence["id"]]
    assert any(item.get("id") == evidence["id"] for item in frozen["assessment_source_manifest"])
    service.delete(evidence["id"])
    assert manager.get(task["id"])["status"] == "INVALIDATED"
    assert manager.self_admission(version["id"])["eligible"] is False
    assert manager._private(task["id"])["snapshot"]["text"] == ""


def test_goal_revocation_invalidates_quality_snapshot(quality):
    service, _, manager, _ = quality
    goal = service.propose("我想练习每天写一段简短的观察记录", "general", "本人批准目标")
    service.directive(goal["id"], goal["revision"], "approve")
    version, task = assess(quality)
    service.directive(goal["id"], goal["revision"] + 1, "revoke")
    assert manager.get(task["id"])["status"] == "INVALIDATED"
    assert manager.self_admission(version["id"])["eligible"] is False


def test_deletion_while_quality_call_inflight_cannot_restore_report(quality):
    service, people, manager, connections = quality
    evidence = people.statement("我喜欢做选择之前核对资料，讲话时重视准确而简短。")
    connections.entered, connections.release = threading.Event(), threading.Event()
    version = people.user_skill()
    task = manager.start_self(version["id"], confirmed=True)
    assert connections.entered.wait(5)
    service.delete(evidence["id"])
    connections.release.set()
    result = finish(manager, task)
    assert result["status"] == "INVALIDATED" and result["report"] is None
    assert manager.self_admission(version["id"])["eligible"] is False


def test_old_strict_grant_or_tampered_quality_report_cannot_admit(quality):
    service, people, manager, _ = quality
    version = people.user_skill()
    with service.store.transaction():
        service.store.add("unlock_grant", {"status": "ACTIVE", "protocol": "strict-personal-v1",
                          "profile_version_id": version["id"], "evidence_ids": [], "assessment_id": "old"})
    assert manager.self_admission()["eligible"] is False
    _, task = assess(quality)
    with service.store.transaction():
        task["report"]["dimensions"][0]["score"] = 20
        service.store.update(task)
    assert manager.self_admission(version["id"])["eligible"] is False


def test_self_quality_api_requires_confirmation_and_derives_subject_type(tmp_path):
    app = create_app(tmp_path / "api")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-WOT-Token"] = client.get("/api/v1/session").json()["token"]
        manager = app.state.upgrade.assessments
        manager.connections.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": canonical({
                "dimensions": {key: {"score": 92, "issues": [], "suggestions": []} for key in QUALITY_LABELS},
                "critical_checks": {"no_permission_escalation": True, "honest_identity": True, "source_declared": True}})},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})))
        manager.connections.save({"provider": "deepseek", "model": "test-only", "api_key": "test-only-key"})
        me = client.post("/api/v1/me", json={"bio": "本人填写的介绍，表达时先说事实，再问一个具体的问题。"}).json()
        version_id = me["draft"]["id"]
        path = f"/api/v1/me/versions/{version_id}/assess"
        assert client.post(path, json={"confirmed": False}).status_code == 400
        assert client.post(path, json={"confirmed": True, "score": 100}).status_code == 400
        task = client.post(path, json={"confirmed": True}, headers={"Idempotency-Key": "assess-owner-once"}).json()
        task = finish(manager, task)
        assert task["subject_type"] == "USER_SKILL" and task["status"] == "PASSED"
        replay = client.post(path, json={"confirmed": True}, headers={"Idempotency-Key": "assess-owner-once"}).json()
        assert replay["id"] == task["id"]
        state = client.get("/api/v1/state").json()["upgrade"]
        assert state["virtual_access"] == {"enabled": True}
        assert state["me"]["self_admission"]["eligible"] is True
        assert state["me"]["self_admission"]["protocol"] == QUALITY_PROTOCOL
        assert state["unlock"]["unlocked"] is False
        assert len(manager.connections.store.all("model_call")) == 1
