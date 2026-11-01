"""Public research contracts use isolated state and in-process Tavily doubles."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from wot.domain import DomainError
from wot.research import ResearchLimit, ResearchManager, public_url
from wot.storage import AppStore

KEY = "tvly-test-secret-62d330590d"
URL = "https://www.example.edu.cn/admissions/2026"
QUERY = "2026 四川 物理 本科 院校 投档位次"


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, secret):
        self.values[service, account] = secret

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def resolver(host, port, **kwargs):
    return [(None, None, None, None, ("9.9.9.9", port))]


def make(tmp_path, handler=None, vault=None, clock=None):
    calls = []

    def dispatch(request):
        calls.append((request.url.path, json.loads(request.content)))
        if handler:
            return handler(request)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "2026年普通本科录取公告",
                "url": URL, "content": "搜索摘要不能当正文。", "published_date": "2026-07-20"}],
                "usage": {"credits": 1}})
        return httpx.Response(200, json={"results": [{"url": URL,
            "raw_content": "2026年 四川 物理 本科\n示例大学 专业组101 最低分560 最低位次40000。\n"}],
            "usage": {"credits": 2}})

    store = AppStore(tmp_path / "research.db")
    service = SimpleNamespace(store=store, lock=threading.RLock(), privacy_revision=0)
    client = httpx.Client(transport=httpx.MockTransport(dispatch))
    manager = ResearchManager(service, tmp_path, http_client=client,
                              credential_backend=vault or Vault(), resolver=resolver, clock=clock)
    return manager, service, calls


def configured(tmp_path, **kwargs):
    manager, service, calls = make(tmp_path, **kwargs)
    assert manager.validate({"api_key": KEY, "remember_key": False})["saved"]
    calls.clear()
    return manager, service, calls


def begin(manager, scope="room", messages=None):
    return manager.begin(scope_kind="CHAT", scope_id=scope, agent_id="actor",
                         generation_id="generation", version_id="version",
                         source_message_ids=messages)


def publish(manager, service, rid, scope="room"):
    with service.store.transaction():
        if not service.store.get(scope):
            service.store.add("conversation", {"title": "isolated"}, rid=scope)
        mid = service.store.add_message(scope, "AGENT", "2026年的数据要按省份和选科对齐。",
                                       request_key=rid)
        manager.publish(rid, mid, 0, "published")
    return mid


def test_config_tests_before_save_and_never_persists_key(tmp_path):
    manager, service, calls = configured(tmp_path)
    config = manager.config()
    assert config["configured"] and config["enabled"]
    assert config["storage"] == "SESSION_ONLY"
    assert config["api_key_mask"] == "••••••••"
    assert KEY not in json.dumps(config)
    assert KEY not in "\n".join(service.store.db.iterdump())
    assert manager.remove()["configured"] is False
    assert manager._session_keys == {}
    assert calls == []


def test_failed_validation_keeps_previous_connection_and_sanitizes_provider_error(tmp_path):
    def handler(request):
        if request.headers["authorization"] == "Bearer " + KEY:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(401, json={"message": "echo: new-key-secret-xxxxxxxx"})

    manager, service, _ = configured(tmp_path, handler=handler)
    revision = manager.config()["revision"]
    result = manager.validate({"api_key": "new-key-secret-xxxxxxxx", "remember_key": False})
    assert result["status"] == "TEST_FAILED" and not result["saved"]
    assert manager.config()["revision"] == revision and manager._key() == KEY
    assert "new-key-secret" not in str(result)
    assert "new-key-secret" not in "\n".join(service.store.db.iterdump())


def test_successful_test_but_failed_os_save_is_explicit_and_preserves_old_config(tmp_path):
    class FailedVault(Vault):
        def set_password(self, *args):
            raise RuntimeError(KEY)

    manager, service, _ = configured(tmp_path, vault=FailedVault())
    old = manager.config()
    result = manager.validate({"api_key": KEY, "remember_key": True})
    assert result["tested"] and not result["saved"] and result["status"] == "SAVE_FAILED"
    assert manager.config() == old
    assert KEY not in str(result) and KEY not in "\n".join(service.store.db.iterdump())


def test_os_credentials_survive_manager_restart_without_readback_or_calls(tmp_path):
    vault = Vault()
    manager, service, calls = make(tmp_path, vault=vault)
    assert manager.validate({"api_key": KEY, "remember_key": True})["saved"]
    calls.clear()
    restarted = ResearchManager(service, tmp_path, credential_backend=vault, http_client=manager.client,
                                 resolver=resolver)
    assert restarted.config()["configured"]
    assert restarted.config()["storage"] == "OS_CREDENTIAL_STORE"
    assert KEY not in str(restarted.config())
    assert calls == []


def test_no_config_never_contacts_network(tmp_path):
    manager, _, calls = make(tmp_path)
    rid = begin(manager)
    context = manager.collect(rid, [QUERY])
    assert context["status"] == "NOT_CONFIGURED" and context["sources"] == []
    assert calls == []


def test_retrieval_reads_original_pages_and_sources_are_only_public_after_publication(tmp_path):
    manager, service, calls = configured(tmp_path)
    rid = begin(manager)
    reserved = []
    context = manager.collect(rid, [QUERY], on_request=reserved.append)
    assert [path for path, _ in calls] == ["/search", "/extract"]
    assert len(set(reserved)) == 2
    source = context["sources"][0]
    assert source["authority"] == "OFFICIAL_DOMAIN" and "40000" in source["excerpt"]
    assert "搜索摘要" not in source["excerpt"]
    assert source["publisher"] == "www.example.edu.cn" and source["applicable_year"] == "2026"
    with pytest.raises(DomainError, match="已发布"):
        manager.sources(rid)
    publish(manager, service, rid)
    assert manager.sources(rid)["sources"] == context["sources"]
    assert QUERY not in "\n".join(service.store.db.iterdump())
    assert len(service.store.all("research_call")) == 3  # separate test + search + read
    assert sum(row["credits"] for row in service.store.all("research_call")) == 4


def test_cache_is_scope_limited_published_only_and_zero_extra_requests(tmp_path):
    manager, service, calls = configured(tmp_path)
    first = begin(manager)
    manager.collect(first, [QUERY])
    # An unposted result cannot become another agent's evidence.
    second = begin(manager)
    manager.collect(second, [QUERY])
    assert len(calls) == 4
    publish(manager, service, second)
    calls.clear()
    third = begin(manager)
    data = manager.collect(third, [QUERY])
    assert data["sources"][0]["cache_hit"] and calls == []
    fourth = begin(manager, "other-room")
    manager.collect(fourth, [QUERY])
    assert len(calls) == 2


def test_expired_cache_is_not_used_as_current_fact(tmp_path):
    manager, service, calls = configured(tmp_path)
    first = begin(manager)
    manager.collect(first, [QUERY])
    publish(manager, service, first)
    with service.store.transaction():
        record = service.store.get(first)
        record["sources"][0]["retrieved_at"] = "2020-01-01T00:00:00+00:00"
        service.store.update(record)
    calls.clear()
    manager.collect(begin(manager), [QUERY])
    assert len(calls) == 2


@pytest.mark.parametrize("url", [
    "http://localhost/a", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://192.168.1.1/", "http://10.0.0.1/",
    "file:///etc/passwd", "ftp://example.com/a", "https://user:pass@example.com/",
    "https://example.com:8765/", "http://example.local/", "http://2130706433/",
    "https://example.com/a?api_key=private", "https://example.com/a?email=person%40example.com",
    "https://example.com\\@127.0.0.1/",
])
def test_private_credentialed_and_non_web_urls_are_rejected(url):
    with pytest.raises(DomainError):
        public_url(url, resolver)


def test_dns_mixed_public_private_answers_and_rebinding_are_rejected():
    def mixed(*args, **kwargs):
        return [(None, None, None, None, ("9.9.9.9", 443)),
                (None, None, None, None, ("10.0.0.1", 443))]
    with pytest.raises(DomainError):
        public_url("https://public.example.com", mixed)
    assert public_url("https://example.com/a#fragment", resolver) == "https://example.com/a"


def test_unread_page_cannot_turn_search_snippet_into_evidence(tmp_path):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": URL, "title": "560分保证录取",
                "content": "伪造摘要：全部保证录取"}, {"url": "http://127.0.0.1/secrets"}]})
        return httpx.Response(200, json={"results": [], "failed_results": [{"url": URL,
            "error": "provider echoed " + KEY}]})
    manager, service, calls = configured(tmp_path, handler=handler)
    result = manager.collect(begin(manager), [QUERY])
    assert result["status"] == "NO_RESULTS" and result["sources"] == []
    assert result["unread_sources"][0]["reason"] == "资料未读取"
    assert "127.0.0.1" not in json.dumps(calls[-1][1])
    assert KEY not in "\n".join(service.store.db.iterdump())


def test_query_privacy_guard_rejects_pii_keys_and_raw_urls(tmp_path):
    manager, _, calls = configured(tmp_path)
    for query in ["张三 13800138000 位次", "name@example.com 录取", KEY, "https://example.com/private"]:
        with pytest.raises(DomainError):
            manager.collect(begin(manager), [query])
    assert calls == []


def test_coordinator_budget_callback_blocks_dispatch_and_is_not_swallowed(tmp_path):
    manager, service, calls = configured(tmp_path)
    class Budget(DomainError):
        reason = "WEB_REQUEST_LIMIT"
    def reserve(call_id):
        raise Budget("budget exhausted", 409)
    with pytest.raises(Budget):
        manager.collect(begin(manager), [QUERY], on_request=reserve)
    assert calls == []
    assert service.store.all("research_call")[-1]["state"] == "NOT_SENT"


def test_two_round_cap_and_three_query_cap_do_not_start_additional_calls(tmp_path):
    manager, _, calls = configured(tmp_path)
    rid = begin(manager)
    manager.collect(rid, [QUERY])
    manager.collect(rid, ["2026 四川 招生计划"])
    count = len(calls)
    with pytest.raises(ResearchLimit):
        manager.collect(rid, ["第三轮不允许"])
    assert len(calls) == count


def test_cancelled_late_search_is_metered_but_never_extracted(tmp_path):
    cancelled = False
    def handler(request):
        nonlocal cancelled
        if json.loads(request.content).get("query") != "Tavily connection test":
            cancelled = True
        return httpx.Response(200, json={"results": [{"url": URL}], "usage": {"credits": 1}})
    manager, service, calls = configured(tmp_path, handler=handler)
    with pytest.raises(DomainError, match="取消"):
        manager.collect(begin(manager), [QUERY], cancelled=lambda: cancelled)
    assert len(calls) == 1
    assert service.store.all("research_call")[-1]["credits"] == 1
    assert service.store.all("research_call")[-1]["discarded"]


def test_key_removal_cancels_pending_but_keeps_published_historical_sources(tmp_path):
    manager, service, _ = configured(tmp_path)
    rid = begin(manager)
    manager.collect(rid, [QUERY])
    publish(manager, service, rid)
    manager.stage(rid, "COMPLETE")
    manager.remove()
    assert manager.stale(rid)
    assert manager.sources(rid)["sources"]
    assert manager.publish(rid, "late-message", 1, "late") == {}


def test_deletion_clears_cache_and_source_cards(tmp_path):
    manager, service, calls = configured(tmp_path)
    rid = begin(manager)
    manager.collect(rid, [QUERY])
    publish(manager, service, rid)
    manager.invalidate_scope("room", "deleted")
    with pytest.raises(DomainError):
        manager.sources(rid)
    assert manager.context(rid)["sources"] == []
    calls.clear()
    manager.collect(begin(manager), [QUERY])
    assert len(calls) == 2


def test_deadline_stops_new_requests_and_reports_incomplete_facts(tmp_path):
    current = [0]
    def handler(request):
        if json.loads(request.content).get("query") != "Tavily connection test":
            current[0] += 46
        return httpx.Response(200, json={"results": [{"url": URL}]})
    manager, _, calls = configured(tmp_path, clock=lambda: current[0], handler=handler)
    rid = begin(manager)
    # Time spent waiting for the model before first retrieval is excluded.
    current[0] = 46
    result = manager.collect(rid, [QUERY])
    assert result["status"] == "FAILED" and "超时" in result["notice"]
    assert len(calls) == 1  # search sent, no extraction after45 active seconds


def test_two_round_deadline_excludes_model_wait_but_sums_active_retrieval_time(tmp_path):
    current = [0]
    def handler(request):
        data = json.loads(request.content)
        if request.url.path == "/search" and data.get("query") == QUERY:
            current[0] += 30
        elif request.url.path == "/search" and data.get("query") == "2026 四川 招生计划":
            current[0] += 16
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": URL}]})
        return httpx.Response(200, json={"results": [{"url": URL, "raw_content": "2026 四川公开招生计划。"}]})
    manager, service, calls = configured(tmp_path, clock=lambda: current[0], handler=handler)
    rid = begin(manager)
    assert manager.collect(rid, [QUERY])["status"] == "READY"
    assert service.store.get(rid)["retrieval_seconds"] == 30
    current[0] += 300  # model reasoning is outside the45-second retrieval budget
    assert manager.collect(rid, ["2026 四川 招生计划"])["status"] == "FAILED"
    assert service.store.get(rid)["retrieval_seconds"] == 46
    assert len(calls) == 3


def test_unread_pages_count_toward_five_page_limit_across_two_rounds(tmp_path):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": f"https://example.com/{i}"} for i in range(5)]})
        return httpx.Response(200, json={"results": []})
    manager, service, calls = configured(tmp_path, handler=handler)
    rid = begin(manager)
    manager.collect(rid, [QUERY])
    manager.collect(rid, ["第二组检索词"])
    assert len([path for path, _ in calls if path == "/extract"]) == 1
    assert service.store.get(rid)["pages"] == 5


def test_forged_official_sounding_host_gets_no_authority_bonus(tmp_path):
    url = "https://exams.zsks.evil.com/2026"
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": url}]})
        return httpx.Response(200, json={"results": [{"url": url, "raw_content": "2026年自称官方的数据。"}]})
    manager, _, _ = configured(tmp_path, handler=handler)
    source = manager.collect(begin(manager), [QUERY])["sources"][0]
    assert source["authority"] == "PUBLIC_SOURCE"


def test_inherited_sources_are_published_fresh_scoped_and_linked_for_deletion(tmp_path):
    manager, service, calls = configured(tmp_path)
    first = begin(manager)
    manager.collect(first, [QUERY])
    second = begin(manager)
    assert not manager.inherit_published(second, [first])["sources"]
    publish(manager, service, first)
    calls.clear()
    assert manager.inherit_published(second, [first])["sources"]
    assert not manager.inherit_published(begin(manager, "other"), [first])["sources"]
    assert calls == []
    manager.invalidate(first, "original evidence withdrawn")
    assert manager.context(second)["sources"] == []


def test_finished_research_notice_is_exposed_without_sources_or_private_tails(tmp_path):
    manager, _, _ = make(tmp_path)
    rid = begin(manager, messages=["human-message"])
    manager.stage(rid, "COMPLETE")
    progress = manager.progress("room")
    assert progress["status"] == "NOT_CONFIGURED"
    assert progress["notice"] and progress["source_message_ids"] == ["human-message"]
    assert "sources" not in progress and "query_hashes" not in progress


def test_publish_does_not_commit_an_outer_transaction(tmp_path):
    manager, service, _ = configured(tmp_path)
    rid = begin(manager)
    manager.collect(rid, [QUERY])
    with pytest.raises(RuntimeError):
        with service.store.transaction():
            manager.publish(rid, "nonexistent", 0, "not yet published")
            raise RuntimeError("rollback")
    assert service.store.get(rid)["publications"] == []


def test_new_process_pauses_interrupted_runs_without_paid_work(tmp_path):
    manager, service, calls = configured(tmp_path)
    rid = begin(manager)
    calls.clear()
    restarted = ResearchManager(service, tmp_path, http_client=manager.client,
                                 credential_backend=manager.vault, resolver=resolver)
    assert service.store.get(rid)["phase"] == "INTERRUPTED"
    assert restarted.context(rid)["sources"] == [] and calls == []


def test_search_service_concurrency_is_limited_to_two(tmp_path):
    active = [0, 0]
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    def handler(request):
        data = json.loads(request.content)
        if data.get("query") != "Tavily connection test":
            with lock:
                active[0] += 1
                active[1] = max(active)
                if active[0] == 2:
                    entered.set()
            release.wait(2)
            with lock:
                active[0] -= 1
        return httpx.Response(200, json={"results": []})
    manager, _, _ = configured(tmp_path, handler=handler)
    runs = [begin(manager, str(i)) for i in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(manager.collect, rid, [QUERY]) for rid in runs]
        assert entered.wait(2)
        assert active[0] == 2
        release.set()
        for future in futures:
            future.result(3)
    assert active[1] == 2


def test_successful_response_cannot_echo_key_into_durable_source_or_metadata(tmp_path):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": URL, "title": KEY,
                "published_date": KEY}]})
        return httpx.Response(200, json={"results": [{"url": URL,
            "raw_content": "2026年公开资料。\n凭据回显：" + KEY}]})
    manager, service, _ = configured(tmp_path, handler=handler)
    context = manager.collect(begin(manager), [QUERY])
    assert context["sources"] and KEY not in json.dumps(context)
    assert KEY not in "\n".join(service.store.db.iterdump())


def test_oversized_response_is_bounded_and_does_not_persist_body(tmp_path):
    def handler(request):
        if json.loads(request.content).get("query") == "Tavily connection test":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, content=b"x" * 3_000_001)
    manager, service, _ = configured(tmp_path, handler=handler)
    result = manager.collect(begin(manager), [QUERY])
    assert result["status"] == "FAILED" and "过大" in result["notice"]
    assert len("\n".join(service.store.db.iterdump())) < 20_000


def test_dns_lookup_has_bounded_wait_without_executing_page_requests():
    release = threading.Event()
    def blocked(*args, **kwargs):
        release.wait(2)
        return resolver(*args, **kwargs)
    try:
        with pytest.raises(DomainError, match="解析"):
            public_url("https://example.com", blocked, timeout=0.01)
    finally:
        release.set()
