import json

import pytest

from dive_memory.service import MemoryService


def approve(service, namespace="u1"):
    proposal = service.propose_hot_profile(namespace)
    return service.review_hot_profile(namespace, proposal["proposal_id"], approve=True, reason="reviewed")


def test_profile_proposal_requires_review_and_preserves_source_events():
    service = MemoryService()
    event = service.ingest("u1", "我住在上海", explicit=True, source_type="user")
    before = service.get_event(event["event_id"])
    proposal = service.propose_hot_profile("u1")
    assert service.hot_profile("u1")["fields"] == {}
    assert proposal["fields"] == {"residence": "上海"}
    assert proposal["provenance"]["residence"][0]["source_event_ids"] == [event["event_id"]]
    result = service.review_hot_profile("u1", proposal["proposal_id"], approve=True, reason="reviewed")
    assert result["revision"] == 1
    assert service.hot_profile("u1")["fields"] == proposal["fields"]
    assert service.get_event(event["event_id"]) == before


def test_new_independent_fact_changes_profile_revision_and_allows_safe_rollback():
    service = MemoryService()
    service.ingest("u1", "我住在上海", explicit=True, source_type="user")
    approve(service)
    service.ingest("u1", "我喜欢绿茶", explicit=True, source_type="user")
    assert service.hot_profile("u1")["stale"]
    assert approve(service)["revision"] == 2
    assert service.rollback_hot_profile("u1", 1, reason="prefer smaller navigation")["revision"] == 3
    assert service.hot_profile("u1")["fields"] == {"residence": "上海"}
    assert len(service.list_memories("u1")) == 2


def test_stale_and_rejected_proposals_do_not_publish():
    service = MemoryService()
    service.ingest("u1", "我住在上海", explicit=True, source_type="user")
    old = service.propose_hot_profile("u1")
    service.ingest("u1", "我喜欢绿茶", explicit=True, source_type="user")
    assert service.review_hot_profile("u1", old["proposal_id"], approve=True, reason="review")["status"] == "STALE"
    fresh = service.propose_hot_profile("u1")
    assert service.review_hot_profile("u1", fresh["proposal_id"], approve=False, reason="reject")["status"] == "REJECTED"
    assert service.hot_profile("u1")["revision"] == 0
    with pytest.raises(ValueError, match="pending"):
        service.review_hot_profile("u1", fresh["proposal_id"], approve=True, reason="retry")


@pytest.mark.parametrize("hard", [False, True])
@pytest.mark.parametrize("by_event", [False, True])
def test_forget_redacts_derived_copies_and_blocks_rollback(hard, by_event):
    service = MemoryService()
    event = service.ingest("u1", "我住在上海", explicit=True, source_type="user")
    approve(service)
    pending = service.propose_hot_profile("u1")
    if by_event:
        service.forget_event(event["event_id"], hard=hard)
    else:
        service.forget(event["memory_ids"][0], hard=hard)
    assert service.hot_profile("u1")["fields"] == {}
    with pytest.raises(ValueError, match="forgotten"):
        service.rollback_hot_profile("u1", 1, reason="restore")
    with pytest.raises(KeyError):
        service.review_hot_profile("u1", pending["proposal_id"], approve=True, reason="restore")
    rows = service.store.db.execute("SELECT * FROM profile_revisions").fetchall()
    assert "上海" not in json.dumps([dict(row) for row in rows], ensure_ascii=False)


def test_review_is_tenant_scoped_and_conflicting_facts_are_omitted():
    service = MemoryService()
    service.ingest("u1", "我喜欢绿茶", explicit=True, source_type="user")
    service.ingest("u1", "我喜欢咖啡", explicit=True, source_type="user")
    service.ingest("u1", "Alice lives in Shanghai", explicit=True, source_type="user")
    proposal = service.propose_hot_profile("u1")
    assert proposal["fields"] == {}
    with pytest.raises(KeyError):
        service.review_hot_profile("u2", proposal["proposal_id"], approve=True, reason="cross tenant")


def test_concurrent_proposals_use_revision_compare_before_publish():
    service = MemoryService()
    service.ingest("u1", "我住在上海", explicit=True, source_type="user")
    p1, p2 = service.propose_hot_profile("u1"), service.propose_hot_profile("u1")
    assert service.review_hot_profile("u1", p1["proposal_id"], approve=True, reason="first")["status"] == "APPROVED"
    assert service.review_hot_profile("u1", p2["proposal_id"], approve=True, reason="second")["status"] == "STALE"
