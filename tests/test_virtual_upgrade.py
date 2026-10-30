"""Scheduler boundary tests: isolated data, fake LIVE transport, no real grants.

FakeUnlock is a test-only capability and is never wired into the application.
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
                "text": "我们可以先从一个具体的小问题开始。",
                "tokens": self.tokens,
                "classification": "LIVE",
                "finish_reason": "stop",
            }
        finally:
            with self.lock:
                self.current -= 1


class FakeUnlock:
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
    unlock = FakeUnlock(people)
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
    return rig.virtual.view(rid)


def publish(rig, rid):
    ready(rig, rid, draft=True)
    rig.virtual._tick()
    return rig.virtual.view(rid)


def self_id(rig):
    return "self:" + rig.unlock.grant["profile_version_id"]


def test_gate_explicit_sharing_distinct_versions_and_no_paid_create(rig):
    rig.unlock.unlocked = False
    with pytest.raises(DomainError):
        create(rig)
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
    assert session["state"] == "COMPLETED"
    assert len(session["turns"]) == 20
    assert session["used_tokens"] == 1000
    rig.virtual._tick()
    assert len(rig.connections.calls) == 20
    small = create(rig, max_tokens=1000)["id"]
    rig.virtual.control(small, "start")
    exhausted = generate(rig, small)
    assert exhausted["state"] == "COMPLETED" and exhausted["draft"] is None
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
    context = json.loads(rig.connections.calls[-1]["messages"][-1]["content"])
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
    assert rig.virtual.view(rid)["state"] == "INVALIDATED"


def test_global_grant_revocation_clears_character_draft_synchronously_preserves_history(rig):
    rid = create(rig)["id"]
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


def test_global_grant_revocation_discards_inflight_character_reply_and_counts_usage(rig):
    rid = create(rig)["id"]
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


def test_global_grant_revocation_cancels_queued_transport_before_it_is_billed(
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
            rid = create(rig)["id"]
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
