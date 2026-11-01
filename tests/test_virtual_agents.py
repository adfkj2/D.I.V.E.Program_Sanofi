"""Open character space, exact SELF admission and resumable actor quotas."""

import json
import threading

import pytest
from test_virtual_upgrade import (
    create,
    generate,
    publish,
    self_id,
    set_burst,
)
from test_virtual_upgrade import (
    rig as rig,
)
from wot.domain import DomainError
from wot.storage import now


def test_character_space_is_open_without_self_assessment_but_self_still_requires_it(rig):
    rig.unlock.unlocked = False
    room = create(rig, start_immediately=True)
    assert room["state"] == "RUNNING"
    assert rig.connections.calls == []
    assert all(p["kind"] == "CHARACTER" for p in rig.virtual.participants())
    assert generate(rig, room["id"])["draft"]
    with pytest.raises(DomainError, match="准入"):
        create(rig, left=self_id(rig))


def test_two_agents_have_persistent_separate_scope_and_version_without_paid_creation(rig):
    first, second = create(rig)["id"], create(rig)["id"]
    actors = rig.service.store.all("agent_instance")
    assert len(actors) == 4 and len({a["id"] for a in actors}) == 4
    assert {a["scope_id"] for a in actors} == {first, second}
    assert all(a["state"] == "IDLE" and not a["pending_messages"] for a in actors)
    assert rig.connections.calls == []
    rig.virtual.control(first, "start")
    generate(rig, first)
    before = dict(rig.virtual._session(first)["agent_ids"])
    publish(rig, first)
    generate(rig, first)
    assert rig.virtual._session(first)["agent_ids"] == before
    assert len(rig.service.store.all("agent_instance")) == 4
    assert {a["scope_id"] for a in rig.service.store.all("agent_call")} == {first}


def test_message_quota_preserves_paid_tail_and_continues_same_agents_after_topup(rig):
    set_burst(rig, "第一条完整的消息。", "第二条完整的消息。", "第三条完整的消息。")
    rid = create(rig, start_immediately=True)["id"]
    generate(rig, rid)
    agents = dict(rig.virtual._session(rid)["agent_ids"])
    with rig.service.store.transaction():
        room = rig.virtual._session(rid)
        room["max_turns"] = 1
        rig.service.store.update(room)
    paused = publish(rig, rid)
    assert paused["state"] == "PAUSED_LIMIT" and paused["pause_reason"] == "MESSAGE_LIMIT"
    saved_id = rig.virtual._session(rid)["draft"]["id"]
    assert rig.virtual.view(rid)["draft"] is None  # hidden character outbox
    assert rig.virtual._session(rid)["burst_pending"]
    rig.virtual.top_up(rid, {"add_messages": 2, "add_tokens": 0})
    rig.virtual._tick()
    assert len(rig.virtual.view(rid)["turns"]) == 1 and len(rig.connections.calls) == 1
    resumed = rig.virtual.control(rid, "resume")
    assert resumed["state"] == "RUNNING"
    assert rig.virtual._session(rid)["draft"]["id"] == saved_id
    publish(rig, rid)
    final = publish(rig, rid)
    assert final["state"] == "PAUSED_LIMIT" and len(final["turns"]) == 3
    assert [t["text"] for t in final["turns"]] == [
        "第一条完整的消息。", "第二条完整的消息。", "第三条完整的消息。",
    ]
    assert rig.virtual._session(rid)["agent_ids"] == agents
    assert len(rig.connections.calls) == 1


def test_budget_topup_is_idempotent_and_does_not_implicitly_resume_or_generate(rig):
    rid = create(rig)["id"]
    body = {"add_messages": 20, "add_tokens": 30_000}
    def once():
        return rig.service.once("budget-one", {"room": rid, **body},
                                lambda: rig.virtual.top_up(rid, body))
    assert once()["max_turns"] == 40
    assert once()["max_tokens"] == 60_000
    assert rig.virtual.view(rid)["state"] == "CREATED"
    assert rig.virtual.view(rid)["used_tokens"] == 0
    assert rig.connections.calls == []
    assert len(rig.service.store.fetchall("SELECT 1 FROM audit WHERE operation='VIRTUAL_BUDGET_ADDED'")) == 1


@pytest.mark.parametrize("data", [
    {}, {"add_messages": True}, {"add_messages": -1}, {"add_messages": 21},
    {"add_tokens": 30_001}, {"add_tokens": "100"}, {"add_messages": 1, "extra": 1},
])
def test_budget_topup_rejects_invalid_additions(rig, data):
    rid = create(rig)["id"]
    with pytest.raises(DomainError):
        rig.virtual.top_up(rid, data)
    assert rig.virtual.view(rid)["max_turns"] == 20


def test_preflight_pause_retains_agent_and_does_not_mark_topic_finished(rig):
    rid = create(rig, max_tokens=1000, start_immediately=True)["id"]
    room = generate(rig, rid)
    assert room["state"] == "PAUSED_LIMIT" and room["pause_reason"] == "BUDGET_PRECHECK"
    assert rig.connections.calls == [] and room["used_tokens"] == 0
    agents = dict(rig.virtual._session(rid)["agent_ids"])
    rig.virtual.top_up(rid, {"add_tokens": 30_000})
    assert rig.connections.calls == []
    rig.virtual.control(rid, "resume")
    assert generate(rig, rid)["draft"]
    assert rig.virtual._session(rid)["agent_ids"] == agents


def test_truncated_and_empty_visible_attempts_are_charged_and_replaced_before_publish(rig, monkeypatch):
    complete = rig.connections.complete
    outputs = [
        {"text": "", "finish_reason": "length", "tokens": 101},
        {"text": '{"messages":["这次是一条完整的发言。"],"idle_followup":null}',
         "finish_reason": "stop", "tokens": 103},
    ]

    def sequence(*args, **kwargs):
        return {**complete(*args, **kwargs), **outputs.pop(0)}

    monkeypatch.setattr(rig.connections, "complete", sequence)
    rid = create(rig, start_immediately=True)["id"]
    room = generate(rig, rid)
    assert room["draft"]["text"] == "这次是一条完整的发言。"
    assert room["used_tokens"] == 204 and len(rig.connections.calls) == 2
    assert [c["max_tokens"] for c in rig.connections.calls] == [2048, 4096]
    assert len(rig.service.store.all("virtual_generation")) == 2
    assert rig.virtual.view(rid)["turns"] == []
    assert publish(rig, rid)["turns"][0]["text"] == "这次是一条完整的发言。"


def test_three_truncations_preserve_checkpoint_and_pause_without_exposing_json(rig, monkeypatch):
    complete = rig.connections.complete

    def cut(*args, **kwargs):
        return {**complete(*args, **kwargs), "text": '{"messages":["未完成', "finish_reason": "max_tokens"}

    monkeypatch.setattr(rig.connections, "complete", cut)
    rid = create(rig, start_immediately=True)["id"]
    room = generate(rig, rid)
    assert room["state"] == "INCOMPLETE" and room["draft"] is None
    assert "本轮发言未完成" in room["failure"]
    assert room["used_tokens"] == 150 and len(rig.connections.calls) == 3
    assert "未完成" not in json.dumps(rig.virtual.view(rid).get("turns"), ensure_ascii=False)
    actor = next(a for a in rig.service.store.all("agent_instance") if a["participant_id"] == "skill_munger")
    assert actor["checkpoint"]["kind"] == "OUTPUT_TRUNCATED"


def test_unrelated_personal_quality_change_does_not_cancel_character_outbox_or_actor(rig):
    rid = create(rig, start_immediately=True)["id"]
    generate(rig, rid)
    before = rig.virtual._session(rid)
    rig.unlock.unlocked = False
    rig.virtual.invalidate("unrelated-self-evidence", "本人质量报告已失效")
    current = rig.virtual._session(rid)
    assert current["state"] == "RUNNING" and current["epoch"] == before["epoch"]
    assert current["draft"]["id"] == before["draft"]["id"]
    assert publish(rig, rid)["turns"]


def test_public_state_and_turns_are_read_from_one_publication_snapshot(rig, monkeypatch):
    rid = create(rig, start_immediately=True)["id"]
    read, attempted, committed = threading.Event(), threading.Event(), threading.Event()
    original = rig.virtual._session
    first_read = True

    def observed_session(room_id):
        nonlocal first_read
        snapshot = original(room_id)
        if threading.current_thread() is threading.main_thread() and first_read:
            first_read = False
            read.set()
            assert attempted.wait(2)
            # An atomic view holds the store lock so publication cannot finish
            # here. Without it, this intentionally exposes the mixed snapshot.
            committed.wait(0.1)
        return snapshot

    def publish_final_turn():
        assert read.wait(2)
        attempted.set()
        with rig.service.store.transaction():
            session = original(rid)
            person = session["participants"][0]
            rig.service.store.add("virtual_turn", {
                "session_id": rid, "speaker_id": person["id"], "text": "一条完整发言。",
                "turn_index": 0, "version_id": person["version_id"],
                "disclosure": "AI 模拟", "published_at": now(),
            })
            session.update(state="PAUSED_LIMIT", pause_reason="MESSAGE_LIMIT", published_count=1)
            rig.service.store.update(session)
        committed.set()

    monkeypatch.setattr(rig.virtual, "_session", observed_session)
    writer = threading.Thread(target=publish_final_turn)
    writer.start()
    try:
        view = rig.virtual.view(rid)
        assert (view["state"], view["published_count"], len(view["turns"])) in {
            ("RUNNING", 0, 0), ("PAUSED_LIMIT", 1, 1),
        }
    finally:
        writer.join(3)
    assert not writer.is_alive() and committed.is_set()
