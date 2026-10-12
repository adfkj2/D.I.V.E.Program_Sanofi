import pytest
from dive_memory.episodic_retention import EpisodicRetentionPolicy
from dive_memory.service import MemoryService
from dive_memory.models import EvidenceState
from dive_memory.gate import GateDecision, GateAction


def reject(text, **kwargs):
    return GateDecision(False, 0, 0, 0, "ephemeral", "test skip", GateAction.SKIP)


def test_episode_does_not_require_durable_fact_promotion():
    s = MemoryService(gate=reject, episodic_policy=EpisodicRetentionPolicy())
    text = "I bought two tickets to London last week. What should I pack?"
    r = s.ingest("u", text, source_type="user", observed_at="2025-05-01T00:00:00Z")
    m = s.get_memory(r["memory_ids"][0])
    assert m.evidence_state is EvidenceState.OBSERVATION
    assert m.structured_content["predicate"] == "episodic_statement"
    start, end = m.structured_content["source_span"]
    assert m.content == text[start:end]
    assert len(r["memory_ids"]) == 1
    assert not s.profile("u").get("residence")
    s.forget_event(r["event_id"])
    assert not s.retrieve("u", "London tickets").items


@pytest.mark.parametrize("text,role", [
    ("If I bought two tickets, would I get a discount?", "user"),
    ("I hope I have three cats next year.", "user"),
    ("Do I have two tickets?", "user"),
    ("I bought three cats yesterday.", "assistant"),
    ("I bought three cats yesterday.", "web"),
    ("I have a password: abc123 and bought two tickets.", "user"),
    ("I have been enjoying things.", "user"),
])
def test_not_an_indiscriminate_archive(text, role):
    assert not EpisodicRetentionPolicy().candidates(text, source_type=role,
                                                   observed_at=None, existing=[])


def test_default_is_unchanged_and_duplicate_sentences_are_bounded():
    text = "I bought two tickets yesterday."
    s = MemoryService(gate=reject)
    assert not s.ingest("u", text, source_type="user")["memory_ids"]
    p = EpisodicRetentionPolicy(max_items=1)
    c = p.candidates(text + " I paid 20 dollars.", source_type="user", observed_at=None, existing=[])
    assert len(c) == 1
    assert not p.candidates(text, source_type="user", observed_at=None, existing=c)


def test_other_namespaces_remain_protected():
    s = MemoryService(gate=reject, episodic_policy=EpisodicRetentionPolicy())
    s.ingest("u", "I bought two London tickets yesterday.", source_type="user")
    assert not s.retrieve("other", "London").items


@pytest.mark.parametrize("deferred", [False, True])
def test_disabled_memory_blocks_episode_projection(deferred):
    s = MemoryService(gate=reject, episodic_policy=EpisodicRetentionPolicy())
    if not deferred:
        s.set_memory_enabled("u", False)
    result = s.ingest("u", "I bought two London tickets yesterday.",
                      source_type="user", defer=deferred)
    if deferred:
        s.set_memory_enabled("u", False)
        assert s.process_pending()[0]["memory_disabled"] is True
    else:
        assert result["memory_disabled"] is True
    assert not s.list_memories("u")
    s.set_memory_enabled("u", True)
    assert not s.process_pending()
    assert not s.retrieve("u", "London tickets").items


def test_two_episodes_coexist_instead_of_superseding():
    s = MemoryService(gate=reject, episodic_policy=EpisodicRetentionPolicy())
    for text in ["I bought two books yesterday.", "I bought three books last week."]:
        s.ingest("u", text, source_type="user")
    assert len(s.list_memories("u")) == 2


def test_decimal_and_person_title_are_not_truncated_into_false_observations():
    text = "I paid $19.99 yesterday. I visited Dr. Smith two weeks ago. What next?"
    cs = EpisodicRetentionPolicy().candidates(text, source_type="user", observed_at=None, existing=[])
    assert [c.content for c in cs] == ["I paid $19.99 yesterday.", "I visited Dr. Smith two weeks ago."]
    for c in cs:
        a, b = c.structured_content["source_span"]
        assert text[a:b] == c.content


def test_nonpositive_limits_are_rejected():
    with pytest.raises(ValueError):
        EpisodicRetentionPolicy(max_items=0)
