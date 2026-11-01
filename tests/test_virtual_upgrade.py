"""Scheduler boundary tests: isolated data, fake LIVE transport, no real admission.

FakeAdmission is a test-only capability and is never wired into the application.
The real assessment tests separately prove that mock scores cannot issue one.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from wot.connections import ConnectionManager
from wot.domain import DomainError
from wot.people import PeopleManager
from wot.service import WotService
from wot.virtual import VirtualSpace, after


class FakeConnections:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = threading.Lock()
        self.current = self.peak = 0
        self.tokens = 50
        self.text = "我们可以先从一个具体的小问题开始。"
        self.failure = None
        self.hash = "test-model-fingerprint"

    def active(self):
        return {"id": "test-connection", "model": "test-model"}

    def config_hash(self, connection_id=None):
        return self.hash

    def is_live(self, connection_id=None):
        return True

    def complete(self, messages, **kwargs):
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
            self.calls.append({"messages": messages, **kwargs})
        self.started.set()
        try:
            assert self.release.wait(4), "test transport was not released"
            if self.failure:
                raise self.failure
            return {
                "text": self.text,
                "tokens": self.tokens,
                "classification": "LIVE",
                "finish_reason": "stop",
            }
        finally:
            with self.lock:
                self.current -= 1


class FakeAdmission:
    def __init__(self, people):
        self.people = people
        self.grant = {"profile_version_id": people.user_skill()["id"]}
        self.unlocked = True

    def unlock_state(self, connection_id=None):
        version = self.people.user_skill(self.grant["profile_version_id"])
        valid = self.unlocked and version["status"] == "ACTIVE"
        return {"unlocked": valid, "grant": self.grant if valid else None}

    def assert_unlocked(self, connection_id=None):
        if not self.unlock_state(connection_id)["unlocked"]:
            raise DomainError("测试许可已失效", 403)
        return self.grant

    def invalidate(self, parent, reason):
        pass

    def self_admission(self, version_id=None):
        version_id = version_id or self.grant["profile_version_id"]
        version = self.people.user_skill(version_id)
        valid = self.unlocked and version_id == self.grant["profile_version_id"] and version["status"] == "ACTIVE"
        return {"eligible": valid, "version_id": version_id,
                "assessment_id": "test-quality-task" if valid else None}

    def assert_self_admitted(self, version_id):
        value = self.self_admission(version_id)
        if not value["eligible"]:
            raise DomainError("测试本人资料准入已失效", 403)
        return value


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("test condition was not reached")


@pytest.fixture
def rig(tmp_path):
    service = WotService(tmp_path)
    people = PeopleManager(service, tmp_path)
    connections = FakeConnections()
    unlock = FakeAdmission(people)
    virtual = VirtualSpace(service, people, connections, unlock)
    service.invalidate_hooks.extend([people.invalidate, virtual.invalidate])
    result = SimpleNamespace(
        service=service, people=people, connections=connections, unlock=unlock, virtual=virtual
    )
    try:
        yield result
    finally:
        connections.release.set()
        virtual.close()
        service.close()


def create(rig, left="skill_munger", right="skill_buffett", **kwargs):
    return rig.virtual.create(
        {
            "left_id": left,
            "right_id": right,
            "topic": "如何学习做长期判断？",
            "sharing_approved": True,
            **kwargs,
        }
    )


def ready(rig, rid, *, draft=False):
    with rig.service.store.transaction():
        session = rig.virtual._session(rid)
        if draft:
            session["draft"]["publish_at"] = after(-1)
        else:
            session["next_at"] = after(-1)
        rig.service.store.update(session)


def generate(rig, rid):
    ready(rig, rid)
    rig.virtual._tick()
    until(lambda: rid not in rig.virtual.inflight)
    return scheduled(rig, rid)


def scheduled(rig, rid):
    """Unit assertions may inspect the private outbox, unlike public APIs."""
    return {**rig.virtual.view(rid), "draft": rig.virtual._session(rid).get("draft")}


def publish(rig, rid):
    ready(rig, rid, draft=True)
    rig.virtual._tick()
    return scheduled(rig, rid)


def self_id(rig):
    return "self:" + rig.unlock.grant["profile_version_id"]


def context_texts(rig):
    texts = []
    for message in rig.connections.calls[-1]["messages"][2:]:
        content = message["content"]
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            value = None
        texts.append(value.get("text", content) if isinstance(value, dict) else content)
    return texts


def test_gate_explicit_sharing_distinct_versions_and_no_paid_create(rig):
    rig.unlock.unlocked = False
    assert create(rig)["state"] == "CREATED"
    with pytest.raises(DomainError):
        create(rig, left=self_id(rig))
    rig.unlock.unlocked = True
    with pytest.raises(DomainError, match="同一"):
        create(rig, right="skill_munger")
    with rig.service.store.transaction():
        role = dict(rig.service.require("skill_munger", "skill"))
        role.pop("id")
        rig.service.store.add("skill", role, rid="same-version-copy")
    with pytest.raises(DomainError, match="同一"):
        create(rig, right="same-version-copy")
    with pytest.raises(DomainError, match="授权"):
        rig.virtual.create({"left_id": "skill_munger", "right_id": "skill_buffett", "topic": "x"})
    session = create(rig)
    assert session["state"] == "CREATED"
    assert session["max_turns"] == 20 and session["max_tokens"] == 30_000
    assert session["cost_label"] == "费用未估算"
    assert rig.connections.calls == []


def test_default_gap_and_self_window_measured_from_actual_publish(rig):
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    generated = generate(rig, rid)
    assert generated["draft"] is not None and not generated["turns"]
    published = publish(rig, rid)
    turn = published["turns"][0]
    gap = (
        datetime.fromisoformat(published["next_at"]) - datetime.fromisoformat(turn["published_at"])
    ).total_seconds()
    assert 12 <= gap <= 25
    assert turn["disclosure"] == "AI 模拟"
    assert turn["version_id"] == published["participants"][0]["version_id"]
    assert "context_corrections" not in published
    own = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(own, "start")
    started = datetime.now(timezone.utc)
    draft = generate(rig, own)["draft"]
    assert (datetime.fromisoformat(draft["publish_at"]) - started).total_seconds() >= 5
    # Other participants cannot be taken over.
    with pytest.raises(DomainError):
        rig.virtual.takeover(rid)


def test_generated_format_prepared_before_draft_once_and_shared_verbatim(rig, monkeypatch):
    complete = rig.connections.complete

    def markdown(*args, **kwargs):
        return {**complete(*args, **kwargs), "text": "# 看法\n**先验证**\n```py\n# comment\nx_y = 2 ** 3\n```"}

    monkeypatch.setattr(rig.connections, "complete", markdown)
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    expected = "看法\n先验证\n# comment\nx_y = 2 ** 3"
    assert generate(rig, rid)["draft"]["text"] == expected
    assert publish(rig, rid)["turns"][0]["text"] == expected
    generate(rig, rid)
    assert expected in context_texts(rig)
    system = rig.connections.calls[-1]["messages"][0]["content"]
    assert "直接询问身份" in system and "不主动提 AI" in system


def test_manual_intervention_is_not_format_cleaned_at_publish_or_next_context(rig):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    rig.virtual.takeover(rid)
    authored = "## 我手写的标题\n**保留标记**\nx_y = 2 ** 3"
    draft = rig.virtual.intervene(rid, {"text": authored, "scope": "TURN_ONLY"})["draft"]
    assert draft["text"] == authored
    assert publish(rig, rid)["turns"][0]["text"] == authored
    generate(rig, rid)
    assert authored in context_texts(rig)
    assert rig.service.active_evidence() == []


def test_empty_format_only_generation_is_incomplete_and_usage_still_charged(rig, monkeypatch):
    complete = rig.connections.complete

    def empty(*args, **kwargs):
        return {**complete(*args, **kwargs), "text": "---"}

    monkeypatch.setattr(rig.connections, "complete", empty)
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    view = generate(rig, rid)
    assert view["draft"] is None and view["state"] == "INCOMPLETE"
    assert view["used_tokens"] == rig.connections.tokens


def test_fair_queue_two_global_calls_and_per_session_lock(rig):
    sessions = [create(rig)["id"] for _ in range(4)]
    for rid in sessions:
        rig.virtual.control(rid, "start")
        ready(rig, rid)
    rig.connections.release.clear()
    rig.virtual._tick()
    until(lambda: len(rig.connections.calls) == 2)
    assert len(rig.virtual.inflight) == 2
    for _ in range(5):
        rig.virtual._tick()
    assert len(rig.connections.calls) == 2  # no second request in one session
    rig.connections.release.set()
    until(lambda: not rig.virtual.inflight)
    rig.virtual._tick()
    until(lambda: not rig.virtual.inflight)
    assert len(rig.connections.calls) == 4
    assert all(rig.virtual._session(rid)["last_dispatched"] for rid in sessions)
    assert rig.connections.peak == 2


def test_service_scheduler_continues_without_any_browser_or_http_client(rig):
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    rig.virtual.start_scheduler()
    until(lambda: len(rig.virtual.view(rid)["turns"]) == 1)
    assert len(rig.connections.calls) == 1
    assert rig.virtual.view(rid)["state"] == "RUNNING"


def test_pause_inflight_discards_output_but_accounts_tokens_and_limit(rig):
    rid = create(rig)["id"]
    rig.connections.tokens = 30_000
    rig.connections.release.clear()
    rig.virtual.control(rid, "start")
    rig.virtual._tick()
    assert rig.connections.started.wait(2)
    rig.virtual.control(rid, "pause")
    rig.connections.release.set()
    until(lambda: not rig.virtual.inflight)
    stopped = rig.virtual.view(rid)
    assert stopped["state"] == "PAUSED"
    assert stopped["used_tokens"] == 30_000
    assert stopped["draft"] is None and stopped["turns"] == []
    with pytest.raises(DomainError, match="限额"):
        rig.virtual.control(rid, "resume")
    rig.virtual._tick()
    assert len(rig.connections.calls) == 1


def test_twenty_turn_stop_and_conservative_total_budget(rig):
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    for _ in range(20):
        assert generate(rig, rid)["draft"]
        publish(rig, rid)
    session = rig.virtual.view(rid)
    assert session["state"] == "PAUSED_LIMIT" and session["pause_reason"] == "MESSAGE_LIMIT"
    assert len(session["turns"]) == 20
    assert session["used_tokens"] == 1000
    rig.virtual._tick()
    assert len(rig.connections.calls) == 20
    small = create(rig, max_tokens=1000)["id"]
    rig.virtual.control(small, "start")
    exhausted = generate(rig, small)
    assert exhausted["state"] == "PAUSED_LIMIT" and exhausted["draft"] is None
    assert len(rig.connections.calls) == 20
    assert "额度不足" in exhausted["failure"]


def test_transport_failure_is_incomplete_and_requires_manual_resume(rig):
    rid = create(rig)["id"]
    rig.connections.failure = DomainError("余额不足", 502)
    rig.virtual.control(rid, "start")
    failed = generate(rig, rid)
    assert failed["state"] == "INCOMPLETE" and "余额" in failed["failure"]
    for _ in range(5):
        rig.virtual._tick()
    assert len(rig.connections.calls) == 1
    rig.connections.failure = None
    rig.virtual.control(rid, "resume")
    assert generate(rig, rid)["draft"]
    assert len(rig.connections.calls) == 2


def test_restart_pauses_incomplete_sessions_and_discards_drafts(rig):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    assert generate(rig, rid)["draft"]
    restarted = VirtualSpace(rig.service, rig.people, rig.connections, rig.unlock)
    try:
        session = restarted.view(rid)
        assert session["state"] == "PAUSED" and session["draft"] is None
        assert "重新启动" in session["failure"]
        restarted._tick()
        assert len(rig.connections.calls) == 1
    finally:
        restarted.close()


def test_turn_only_feedback_does_not_update_real_memory(rig):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    original = generate(rig, rid)["draft"]
    rig.virtual.takeover(rid)
    changed = rig.virtual.intervene(rid, {"text": "只在这一轮这样表达。", "scope": "TURN_ONLY"})
    assert changed["draft"]["id"] == original["id"]
    assert changed["draft"]["text"] == "只在这一轮这样表达。"
    assert rig.service.active_evidence() == []
    assert rig.service.store.all("directive") == []
    assert rig.service.store.all("intervention")[0]["source_id"] is None


def test_real_intervention_new_draft_keeps_certified_version_and_source_trace(rig):
    certified = rig.unlock.grant["profile_version_id"]
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    rig.virtual.takeover(rid)
    with pytest.raises(DomainError, match="明确确认"):
        rig.virtual.intervene(rid, {"text": "我喜欢先留出风险余地。", "scope": "REAL_VIEW"})
    changed = rig.virtual.intervene(
        rid, {"text": "我喜欢先留出风险余地。", "scope": "REAL_VIEW", "confirmed": True}
    )
    assert changed["draft"]["version_id"] == certified
    assert rig.unlock.grant["profile_version_id"] == certified
    assert rig.people.user_skill()["id"] != certified
    evidence = rig.service.active_evidence()
    assert len(evidence) == 1 and evidence[0]["origin"] == "USER_CORRECTION"
    event = rig.service.store.all("intervention")[0]
    assert event["source_id"] == evidence[0]["id"] and event["participant_version"] == certified
    publish(rig, rid)
    generate(rig, rid)
    context = json.loads(rig.connections.calls[-1]["messages"][1]["content"])
    assert context["human_confirmed_current_corrections"][0]["evidence_id"] == evidence[0]["id"]
    assert "SELF" not in rig.connections.calls[-1]["messages"][0]["content"]
    rig.service.delete(evidence[0]["id"])
    invalid = rig.virtual.view(rid)
    assert invalid["state"] == "INVALIDATED" and invalid["draft"] is None
    assert invalid["turns"][0]["text"] == "[依赖来源已撤回]"
    assert rig.virtual._session(rid)["context_corrections"] == []
    assert "先留出风险余地" not in json.dumps(
        rig.service.store.all("intervention"), ensure_ascii=False
    )


def test_ideal_intervention_is_chosen_not_current_fact(rig):
    certified = rig.unlock.grant["profile_version_id"]
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    rig.virtual.takeover(rid)
    rig.virtual.intervene(
        rid, {"text": "我希望练习更耐心地听完。", "scope": "IDEAL_DIRECTION", "confirmed": True}
    )
    assert rig.service.active_evidence() == []
    directive = rig.service.store.all("directive")[0]
    assert directive["state"] == "APPROVED" and directive["origin"] == "USER_CHOSEN_DIRECTIVE"
    assert rig.people.user_skill()["id"] != certified
    assert rig.unlock.grant["profile_version_id"] == certified
    assert directive["id"] in rig.people.user_skill()["directive_ids"]
    rig.service.directive(directive["id"], directive["revision"], "revoke")
    assert rig.virtual.view(rid)["state"] == "INVALIDATED"


def test_character_removal_stops_new_generation_preserves_published_history(rig):
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    original = publish(rig, rid)["turns"][0]["text"]
    rig.people.delete("skill_munger")
    rig.virtual.invalidate("skill_munger", "人物已移出可用列表")
    session = rig.virtual.view(rid)
    assert session["state"] == "INVALIDATED"
    assert session["turns"][0]["text"] == original
    with pytest.raises(DomainError):
        rig.virtual.control(rid, "resume")


def test_model_change_and_revoked_grant_cannot_publish_inflight(rig):
    rid = create(rig)["id"]
    rig.connections.release.clear()
    rig.virtual.control(rid, "start")
    rig.virtual._tick()
    assert rig.connections.started.wait(2)
    rig.connections.hash = "new-model"
    rig.connections.release.set()
    until(lambda: not rig.virtual.inflight)
    assert rig.virtual.view(rid)["draft"] is None
    assert rig.virtual.view(rid)["used_tokens"] == 50
    rig.connections.hash = "test-model-fingerprint"
    rig.unlock.unlocked = False
    rig.virtual.invalidate("some-evidence", "本人许可已撤销")
    assert rig.virtual.view(rid)["state"] == "INCOMPLETE"


def test_voice_revision_during_generation_never_rebinds_a_pinned_participant(rig):
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    first = generate(rig, rid)["draft"]
    published = publish(rig, rid)["turns"][0]
    rig.connections.release.clear()
    ready(rig, rid)
    rig.virtual._tick()
    until(lambda: len(rig.connections.calls) == 2)
    with rig.service.store.transaction():
        role = rig.service.require("skill_buffett", "skill")
        role.update(text="新版本的完整人物资料。", version="next-voice")
        rig.service.store.update(role)
    rig.connections.release.set()
    until(lambda: not rig.virtual.inflight)
    view = rig.virtual.view(rid)
    assert view["draft"] is None and view["state"] == "INCOMPLETE"
    assert "版本" in view["failure"]
    assert view["turns"][0] == published
    assert view["turns"][0]["version_id"] == first["version_id"]
    assert view["used_tokens"] == 100
    with pytest.raises(DomainError, match="版本"):
        rig.virtual.control(rid, "resume")


def test_self_admission_revocation_clears_draft_synchronously_preserves_history(rig):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    published = publish(rig, rid)["turns"][0]["text"]
    assert generate(rig, rid)["draft"] is not None
    epoch = rig.virtual._session(rid)["epoch"]
    rig.unlock.unlocked = False
    rig.virtual.invalidate("unrelated-personal-grant-source", "本人校验许可已撤销")
    # No scheduler tick is needed to close the gate and clear unpublished text.
    invalid = rig.virtual.view(rid)
    assert invalid["state"] == "INVALIDATED"
    assert invalid["draft"] is None and invalid["typing"] is False
    assert rig.virtual._session(rid)["epoch"] > epoch
    assert invalid["turns"][0]["text"] == published
    rig.virtual._tick()
    assert len(rig.connections.calls) == 2


def test_self_admission_revocation_discards_inflight_reply_and_counts_usage(rig):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    published = publish(rig, rid)["turns"][0]["text"]
    rig.connections.started.clear()
    rig.connections.release.clear()
    ready(rig, rid)
    rig.virtual._tick()
    assert rig.connections.started.wait(2)
    rig.unlock.unlocked = False
    rig.virtual.invalidate("unrelated-personal-grant-source", "本人校验许可已撤销")
    assert rig.virtual.view(rid)["state"] == "INVALIDATED"
    rig.connections.release.set()
    until(lambda: not rig.virtual.inflight)
    invalid = rig.virtual.view(rid)
    assert invalid["state"] == "INVALIDATED"
    assert invalid["draft"] is None and invalid["typing"] is False
    assert invalid["used_tokens"] == 100  # discarded output is still billed
    assert len(invalid["turns"]) == 1 and invalid["turns"][0]["text"] == published
    rig.virtual._tick()
    assert len(rig.connections.calls) == 2


def test_self_admission_revocation_cancels_queued_transport_before_it_is_billed(
    rig, tmp_path, monkeypatch
):
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    requests = []

    def transport(request):
        requests.append(request)
        if len(requests) == 2:
            entered.set()
        assert release.wait(4)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "fixture"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            },
        )

    manager = ConnectionManager(
        rig.service.store,
        tmp_path,
        credential_backend=object(),
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    manager.save(
        {
            "provider": "deepseek",
            "model": "test-model",
            "api_key": "not-a-real-key-test",
            "remember": False,
        }
    )
    rig.virtual.connections = manager
    original_complete = manager.complete

    def observe_queue(*args, **kwargs):
        if kwargs.get("purpose") == "virtual":
            queued.set()
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(manager, "complete", observe_queue)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            blockers = [
                pool.submit(manager.complete, [{"role": "user", "content": "block slot"}])
                for _ in range(2)
            ]
            assert entered.wait(2)
            rid = create(rig, left=self_id(rig))["id"]
            rig.virtual.control(rid, "start")
            rig.virtual._tick()
            assert queued.wait(2)
            rig.unlock.unlocked = False
            rig.virtual.invalidate("unrelated-personal-grant-source", "本人校验许可已撤销")
            until(lambda: not rig.virtual.inflight)
            invalid = rig.virtual.view(rid)
            assert invalid["state"] == "INVALIDATED" and invalid["draft"] is None
            assert invalid["used_tokens"] == 0  # no request was sent, so no virtual charge
            assert len(requests) == 2
            release.set()
            assert all(blocker.result(timeout=2)["tokens"] == 25 for blocker in blockers)
    finally:
        release.set()
        manager.close()


def test_pause_all_and_cosmetic_updates_do_not_change_participant_versions(rig):
    own = create(rig, left=self_id(rig))["id"]
    other = create(rig)["id"]
    version = rig.virtual.view(own)["participants"][0]["version_id"]
    for rid in [own, other]:
        rig.virtual.control(rid, "start")
    rig.people.profile({"nickname": "新昵称"})
    value = rig.service.require("skill_buffett", "skill")
    with rig.service.store.transaction():
        value["avatar_url"] = "/user-avatars/new.png"
        rig.service.store.update(value)
    assert rig.virtual.view(own)["participants"][0]["display_name"] == "新昵称"
    assert rig.virtual.view(own)["participants"][0]["version_id"] == version
    assert rig.virtual.view(other)["participants"][1]["avatar_url"] == "/user-avatars/new.png"
    rig.virtual.pause_all()
    assert all(session["state"] == "PAUSED" for session in rig.virtual.list())


def test_intervention_does_not_hold_virtual_lock_while_waiting_for_service_lock(rig, monkeypatch):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    rig.virtual.takeover(rid)
    entered = threading.Event()
    original = rig.people.statement
    errors = []

    def waiting_statement(*args):
        entered.set()
        return original(*args)

    def intervene():
        try:
            rig.virtual.intervene(
                rid, {"text": "我喜欢谨慎考虑。", "scope": "REAL_VIEW", "confirmed": True}
            )
        except DomainError as exc:
            errors.append(exc)

    monkeypatch.setattr(rig.people, "statement", waiting_statement)
    with rig.service.lock:
        thread = threading.Thread(target=intervene, daemon=True)
        thread.start()
        assert entered.wait(2)
        # Deletion's callback must finish while the human write waits for the
        # service lock, rather than deadlocking on virtual.lock.
        with rig.service.store.transaction():
            rig.virtual.invalidate("skill_buffett", "人物已移出")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert rig.virtual.view(rid)["state"] == "INVALIDATED"
    assert errors  # intervention never resurrects the invalidated draft


def test_deleted_current_correction_is_checked_even_without_callback(rig):
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    rig.virtual.takeover(rid)
    rig.virtual.intervene(
        rid, {"text": "我喜欢认真比较。", "scope": "REAL_VIEW", "confirmed": True}
    )
    source = rig.service.active_evidence()[0]
    with rig.service.store.transaction():
        rig.service.store.db.execute(
            "UPDATE evidence SET status='DELETE_PENDING',text='' WHERE id=?", (source["id"],)
        )
    assert publish(rig, rid)["state"] == "INVALIDATED"
    assert rig.virtual.view(rid)["turns"] == []


def test_private_self_skill_never_sent_to_other_actor_or_pure_character_chat(rig):
    canary = "PRIVATE_SELF_VERSION_CANARY_5961"
    rig.people.profile({"bio": canary})
    rig.unlock.grant["profile_version_id"] = rig.people.user_skill()["id"]
    own = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(own, "start")
    generate(rig, own)
    assert canary in rig.connections.calls[-1]["messages"][0]["content"]
    publish(rig, own)
    generate(rig, own)
    assert canary not in json.dumps(rig.connections.calls[-1]["messages"])
    pure = create(rig)["id"]
    rig.virtual.control(pure, "start")
    generate(rig, pure)
    publish(rig, pure)
    generate(rig, pure)
    assert canary not in json.dumps(rig.connections.calls[-1]["messages"])
    assert rig.service.active_evidence() == []
    assert rig.service.store.all("intervention") == []


def set_burst(rig, *parts):
    rig.connections.text = json.dumps({"messages": list(parts), "idle_followup": None},
                                      ensure_ascii=False)


def test_three_part_burst_keeps_speaker_then_hands_off_once(rig):
    set_burst(rig, "先说一个小事。", "我倒想问，哪里最让你在意？", "慢慢说。")
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    first = generate(rig, rid)
    assert first["typing_speaker_id"] == "skill_munger"
    assert "burst_pending" not in first and "burst_generation_id" not in first
    assert len(rig.virtual._session(rid)["burst_pending"]) == 2
    for expected in (1, 2, 3):
        before = datetime.now(timezone.utc)
        view = publish(rig, rid)
        assert len(view["turns"]) == expected
        if expected < 3:
            gap = (datetime.fromisoformat(view["draft"]["publish_at"]) - before).total_seconds()
            assert 1 <= gap <= 4.1
            assert view["draft"]["speaker_id"] == "skill_munger"
            assert view["typing"] is True
    assert len(rig.connections.calls) == 1
    assert {t["speaker_id"] for t in view["turns"]} == {"skill_munger"}
    assert [t["part_index"] for t in view["turns"]] == [0, 1, 2]
    assert len({t["generation_id"] for t in view["turns"]}) == 1
    assert not any("tokens" in t for t in view["turns"])
    assert view["used_tokens"] == 50
    generations = rig.service.store.all("virtual_generation")
    assert len(generations) == 1 and generations[0]["tokens"] == 50
    gap = (datetime.fromisoformat(view["next_at"]) -
           datetime.fromisoformat(view["turns"][-1]["published_at"])).total_seconds()
    assert 12 <= gap <= 25
    next_group = generate(rig, rid)
    assert next_group["draft"]["speaker_id"] == "skill_buffett"
    assert context_texts(rig) == [
        "先说一个小事。", "我倒想问，哪里最让你在意？", "慢慢说。",
    ]


def test_character_current_draft_and_tail_stay_private_until_publish(rig):
    set_burst(rig, "CHARACTER_FIRST_UNPUBLISHED", "CHARACTER_SECOND_UNPUBLISHED",
              "CHARACTER_THIRD_UNPUBLISHED")
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    public = rig.virtual.view(rid)
    assert public["draft"] is None and public["typing"] is True
    assert "CHARACTER_FIRST_UNPUBLISHED" not in json.dumps(public)
    assert "CHARACTER_SECOND_UNPUBLISHED" not in json.dumps(public)
    publish(rig, rid)
    public = rig.virtual.view(rid)
    assert public["draft"] is None and public["typing"] is True
    assert public["turns"][0]["text"] == "CHARACTER_FIRST_UNPUBLISHED"
    assert "CHARACTER_SECOND_UNPUBLISHED" not in json.dumps(public)
    assert "CHARACTER_THIRD_UNPUBLISHED" not in json.dumps(public)
    assert all(item["draft"] is None for item in rig.virtual.list())


def test_remaining_single_slot_clamps_burst_and_counts_bubbles(rig):
    rid = create(rig, max_turns=2)["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    publish(rig, rid)
    set_burst(rig, "最后一条。", "越过名额不能出现。", "也不出现。")
    generate(rig, rid)
    view = publish(rig, rid)
    assert len(view["turns"]) == 2 and view["state"] == "PAUSED_LIMIT"
    assert view["turns"][-1]["text"] == "最后一条。"
    assert rig.virtual._session(rid)["burst_pending"] == []
    assert "最多1条消息" in rig.connections.calls[-1]["messages"][0]["content"]


def test_every_self_part_has_five_seconds_and_takeover_discards_tail(rig):
    set_burst(rig, "第一句。", "第二句。", "不该发布的第三句。")
    rid = create(rig, left=self_id(rig))["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    before = datetime.now(timezone.utc)
    second = publish(rig, rid)
    assert second["draft"]["text"] == "第二句。"
    assert (datetime.fromisoformat(second["draft"]["publish_at"]) - before).total_seconds() >= 5
    rig.virtual.takeover(rid)
    assert rig.virtual._session(rid)["burst_pending"] == []
    authored = "**这是我手写的第二句**"
    before = datetime.now(timezone.utc)
    modified = rig.virtual.intervene(rid, {"text": authored, "scope": "TURN_ONLY"})
    assert (datetime.fromisoformat(modified["draft"]["publish_at"]) - before).total_seconds() >= 5
    view = publish(rig, rid)
    assert [t["text"] for t in view["turns"]] == ["第一句。", authored]
    assert view["draft"] is None
    assert generate(rig, rid)["draft"]["speaker_id"] == "skill_buffett"
    assert "不该发布" not in json.dumps(context_texts(rig), ensure_ascii=False)
    assert rig.service.active_evidence() == []


def test_exact_token_limit_finishes_already_generated_burst_without_another_call(rig):
    set_burst(rig, "第一句。", "第二句。", "第三句。")
    rig.connections.tokens = 30_000
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    assert publish(rig, rid)["state"] == "RUNNING"
    assert publish(rig, rid)["state"] == "RUNNING"
    view = publish(rig, rid)
    assert view["state"] == "PAUSED_LIMIT" and len(view["turns"]) == 3
    assert view["used_tokens"] == 30_000
    ready(rig, rid)
    rig.virtual._tick()
    assert len(rig.connections.calls) == 1


@pytest.mark.parametrize("action", ["pause", "stop", "invalidate", "restart"])
def test_unpublished_burst_tail_cleared_on_every_stop_boundary(rig, action):
    set_burst(rig, "已经发布。", "TAIL_PRIVATE_CANARY", "尚未发出。")
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    publish(rig, rid)
    assert rig.virtual._session(rid)["burst_pending"]
    public = rig.virtual.view(rid)
    assert "尚未发出" not in json.dumps(public, ensure_ascii=False)
    if action in {"pause", "stop"}:
        rig.virtual.control(rid, action)
    elif action == "invalidate":
        rig.virtual.invalidate("skill_munger", "人物已移出")
    else:
        restarted = VirtualSpace(rig.service, rig.people, rig.connections, rig.unlock)
        restarted.close()
    session = rig.virtual._session(rid)
    assert session["burst_pending"] == [] and session["draft"] is None
    assert not session["typing"]
    assert [t["text"] for t in rig.virtual.view(rid)["turns"]] == ["已经发布。"]


def test_legacy_single_turn_migrates_next_speaker_without_modifying_history(rig):
    rid = create(rig)["id"]
    rig.virtual.control(rid, "start")
    generate(rig, rid)
    old_turn = publish(rig, rid)["turns"][0]
    rig.virtual.control(rid, "pause")
    with rig.service.store.transaction():
        session = rig.virtual._session(rid)
        for field in ("next_speaker_index", "burst_pending", "burst_published_count"):
            session.pop(field, None)
        rig.service.store.update(session)
    restarted = VirtualSpace(rig.service, rig.people, rig.connections, rig.unlock)
    try:
        assert restarted._session(rid)["next_speaker_index"] == 1
        assert restarted.view(rid)["turns"] == [old_turn]
    finally:
        restarted.close()
