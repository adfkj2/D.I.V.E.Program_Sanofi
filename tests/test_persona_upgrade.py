"""Curated updates preserve local ownership and dialogue keeps honest boundaries."""

import hashlib
import json
from pathlib import Path

import pytest
from wot.catalog import builtin_people, builtin_upgrade, role_context
from wot.model import Provider
from wot.service import WotService
from wot.voice import SCENES, compile_voice, extract_voice

PRIOR = json.loads((Path(__file__).parent / "fixtures/managed_people_v1_1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("person", builtin_people(), ids=lambda person: person["slug"])
def test_curated_personas_have_concrete_reasoning_voice_and_connected_examples(person):
    text = person["text"]
    profile = extract_voice(text)
    assert profile["basis"]["validation_state"] == "UNVALIDATED"
    assert {example["scene"] for example in profile["examples"]} == set(SCENES)
    assert all(t["evidence_ids"] for t in profile["traits"])
    runtime = role_context(person)["untrusted_role_data"]
    assert runtime == compile_voice(profile)
    assert all(t["description"] in runtime for t in profile["traits"])
    assert len(text.encode()) <= 64000 and len(runtime) <= 10000


def test_upgrade_only_changes_exact_managed_body_and_version(tmp_path):
    person = builtin_people()[0]
    old_text = next(p["text"] for p in PRIOR if p["id"] == person["id"])
    assert hashlib.sha256(old_text.encode()).hexdigest() == person["supersedes"]["text_sha256"]
    old = {
        **person,
        "text": old_text,
        "version": person["supersedes"]["version"],
        "status": "DELETED",
        "deleted_at": "original-tombstone",
        "avatar_url": "/user-avatars/custom.png",
        "display_name": "我取的昵称",
    }
    service = WotService(tmp_path)
    conversation = service.conversation("原来的聊天", person["id"])
    with service.store.transaction():
        service.store.update({**service.require(person["id"], "skill"), **old})
    service.close()
    restarted = WotService(tmp_path)
    try:
        updated = restarted.require(person["id"], "skill")
        assert updated["text"] == person["text"] and updated["version"] == person["version"]
        for key in ("status", "deleted_at", "avatar_url", "display_name", "id", "source_version"):
            assert updated[key] == old[key]
        assert restarted.require(conversation["id"], "conversation")["skill_id"] == person["id"]
        assert builtin_upgrade(updated, person) is None
    finally:
        restarted.close()


@pytest.mark.parametrize("override", [
    {"text": "我自己改过的人物内容"}, {"builtin": False},
    {"version": 42}, {"source_version": "another-source"}, {"slug": "custom"},
])
def test_upgrade_never_overwrites_local_or_unknown_bodies(override):
    person = builtin_people()[0]
    old_text = next(p["text"] for p in PRIOR if p["id"] == person["id"])
    existing = {**person, "version": person["supersedes"]["version"], "text": old_text, **override}
    assert builtin_upgrade(existing, person) is None


def test_mock_speech_uses_selected_authored_samples_and_direct_identity_is_honest(monkeypatch):
    monkeypatch.setenv("WOT_PROVIDER", "mock")
    provider = Provider()
    first_replies = []
    for person in builtin_people():
        replies, transcript = [], []
        for topic in ("我该怎么选择", "我还是很犹豫", "你再说说看"):
            reply = provider.generate(role="EXTERNAL", topic=topic, transcript=transcript,
                                      private=[], skill=role_context(person), goals=[])
            replies.append(reply["text"])
            transcript.extend([{"speaker": "USER", "text": topic},
                               {"speaker": "AGENT", "text": reply["text"]}])
        context = json.loads(role_context(person)["untrusted_role_data"])
        authored = {turn["text"] for ex in context["场景示范"] for turn in ex["turns"] if turn["speaker"] == "role"}
        assert set(replies) <= authored  # Demo samples are not live fidelity evidence.
        assert not any("离线演示" in text or "非本人" in text or "AI" in text for text in replies)
        first_replies.append(replies[0])
        identity = provider.generate(role="EXTERNAL", topic="你是不是AI？", transcript=transcript,
                                     private=[], skill=role_context(person), goals=[])
        assert "AI 角色" in identity["text"] and "不是人物本人" in identity["text"]
    assert len(set(first_replies)) == 15
    assert provider.external_calls == 0
