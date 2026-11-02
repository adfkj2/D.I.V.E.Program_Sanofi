"""Deletion and pre-dispatch quota races use isolated stores and HTTP doubles."""

from contextlib import contextmanager

import httpx
import pytest
from test_domestic_research import BODY, KEYS, QUERY, answer
from test_research import begin, make, publish, resolver
from test_virtual_upgrade import create
from test_virtual_upgrade import rig as rig
from wot.domain import DomainError
from wot.research import ResearchManager
from wot.service import WotService
from wot.storage import now


@pytest.fixture(params=["tavily", "deepseek"])
def research(request, tmp_path):
    manager, service, calls = make(tmp_path, handler=answer)
    provider = request.param
    key = "tvly-isolated-review-credential" if provider == "tavily" else KEYS[provider]
    assert manager.validate({"provider": provider, "api_key": key, "remember_key": False})["saved"]
    yield manager, service, calls
    manager.client.close()
    manager.close()
    service.store.db.close()


def assert_purged(service, rid):
    run = service.store.get(rid)
    assert run["phase"] == "INVALIDATED"
    for key in ("sources", "unread_sources", "query_hashes", "publications", "requested_urls"):
        assert not run.get(key)


def test_withdrawal_after_cache_selection_cannot_restore_durable_excerpts(research):
    manager, service, _ = research
    parent = begin(manager)
    assert manager.collect(parent, [QUERY])["sources"]
    publish(manager, service, parent)
    child = begin(manager)
    original = manager._cache

    def revoke_after_selection(run, query_hash):
        selected = original(run, query_hash)
        assert selected
        manager.invalidate_scope("room", "依赖来源已撤回")
        return selected

    manager._cache = revoke_after_selection
    with pytest.raises(DomainError) as error:
        manager.collect(child, [QUERY])
    assert error.value.status == 409
    assert_purged(service, child)
    assert_purged(service, parent)
    assert BODY not in "\n".join(service.store.db.iterdump())


def test_inheritance_rechecks_parent_and_current_run_inside_deletion_lock(research):
    manager, service, _ = research
    parent = begin(manager)
    assert manager.collect(parent, [QUERY])["sources"]
    publish(manager, service, parent)
    child = begin(manager)
    atomic, pending = manager._atomic, [True]

    @contextmanager
    def revoke_before_lock():
        if pending[0]:
            pending[0] = False
            manager.invalidate_scope("room", "依赖来源已撤回")
        with atomic():
            yield

    manager._atomic = revoke_before_lock
    assert manager.inherit_published(child, [parent])["status"] == "CANCELLED"
    assert_purged(service, child)
    assert_purged(service, parent)


def test_withdrawal_before_initial_transition_cannot_restore_old_run_snapshot(research):
    manager, service, calls = research
    rid = begin(manager)
    before = len(calls)
    atomic, pending = manager._atomic, [True]

    @contextmanager
    def revoke_before_lock():
        if pending[0]:
            pending[0] = False
            manager.invalidate_scope("room", "依赖来源已撤回")
        with atomic():
            yield

    manager._atomic = revoke_before_lock
    with pytest.raises(DomainError) as error:
        manager.collect(rid, [QUERY])
    assert error.value.status == 409
    assert len(calls) == before
    assert_purged(service, rid)


def test_late_network_error_cannot_replace_a_withdrawn_state_with_failed(research):
    manager, service, _ = research
    rid = begin(manager)

    def fail_after_withdrawal(request):
        manager.invalidate_scope("room", "依赖来源已撤回")
        raise httpx.ReadError("isolated network interruption", request=request)

    manager.client.close()
    manager.client = httpx.Client(transport=httpx.MockTransport(fail_after_withdrawal))
    with pytest.raises(DomainError) as error:
        manager.collect(rid, [QUERY])
    assert error.value.status == 409
    assert_purged(service, rid)


def test_inherited_search_summaries_remain_snippets_only(tmp_path):
    manager, service, _ = make(tmp_path, handler=answer)
    try:
        assert manager.validate({"provider": "bocha", "api_key": "bocha-isolated-review-credential"})["saved"]
        parent = begin(manager)
        assert manager.collect(parent, [QUERY])["status"] == "SNIPPETS_ONLY"
        publish(manager, service, parent)
        child = begin(manager)
        inherited = manager.inherit_published(child, [parent])
        assert inherited["status"] == "SNIPPETS_ONLY"
        assert inherited["sources"] and all(not s["verified_body"] for s in inherited["sources"])
    finally:
        manager.client.close()
        manager.close()
        service.store.db.close()


def test_startup_cleans_legacy_research_for_an_already_deleted_chat(tmp_path):
    service = WotService(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(answer))
    manager = ResearchManager(service, tmp_path, http_client=client, resolver=resolver)
    try:
        assert manager.validate({"provider": "tavily", "api_key": "tvly-isolated-review-credential"})["saved"]
        room = service.conversation("旧版已删除的聊天", "skill_munger")["id"]
        rid = begin(manager, room)
        assert manager.collect(rid, [QUERY])["sources"]
        publish(manager, service, rid, room)
        with service.store.transaction():
            conversation = service.require(room, "conversation")
            conversation["deleted_at"] = now()
            service.store.update(conversation)
        restarted = ResearchManager(service, tmp_path, http_client=client, resolver=resolver)
        assert_purged(service, rid)
        assert service.messages(room)[-1]["research_id"] is None
        assert not service.messages(room)[-1]["source_refs"]
        snapshot = service.store.get(rid)
        ResearchManager(service, tmp_path, http_client=client, resolver=resolver)
        assert service.store.get(rid) == snapshot
        restarted.close()
    finally:
        client.close()
        manager.close()
        service.close()


@pytest.mark.parametrize("sent", [False, True])
def test_virtual_quota_refunds_only_requests_cancelled_before_dispatch(rig, tmp_path, sent):
    outbound = []

    def dispatch(request):
        outbound.append(request.url.path)
        if sent and len(outbound) > 1:
            raise httpx.ReadTimeout("result unknown after dispatch", request=request)
        return answer(request)

    client = httpx.Client(transport=httpx.MockTransport(dispatch))
    manager = ResearchManager(rig.service, tmp_path, http_client=client, resolver=resolver)
    try:
        assert manager.validate({"provider": "tavily", "api_key": "tvly-isolated-review-credential"})["saved"]
        room = create(rig, start_immediately=True, max_web_requests=1)["id"]
        with rig.service.store.transaction():
            session = rig.virtual._session(room)
            session["state"] = "GENERATING"
            rig.service.store.update(session)
        epoch = session["epoch"]
        rid = manager.begin(scope_kind="VIRTUAL", scope_id=room, agent_id="isolated-agent",
            generation_id="isolated-generation", version_id=session["participants"][0]["version_id"])
        cancelled, settlers = [False], []

        def reserve(request_id):
            settle = rig.virtual._reserve_web_request(room, epoch, request_id)
            settlers.append(settle)
            if not sent:
                cancelled[0] = True
            return settle

        if sent:
            assert manager.collect(rid, [QUERY], cancelled=lambda: cancelled[0], on_request=reserve)["status"] == "FAILED"
        else:
            with pytest.raises(DomainError):
                manager.collect(rid, [QUERY], cancelled=lambda: cancelled[0], on_request=reserve)
        assert len(outbound) == (2 if sent else 1)
        record = rig.service.store.all("virtual_web_reservation")[-1]
        call = rig.service.store.all("research_call")[-1]
        assert record["state"] == ("DISPATCHED" if sent else "NOT_SENT")
        assert call["state"] == ("FAILED" if sent else "NOT_SENT")
        assert rig.virtual.view(room)["used_web_requests"] == int(sent)
        assert rig.service.store.get(rid)["web_requests"] == int(sent)
        settlers[0](False)
        settlers[0](False)
        assert rig.virtual.view(room)["used_web_requests"] == int(sent)
    finally:
        client.close()
        manager.close()
