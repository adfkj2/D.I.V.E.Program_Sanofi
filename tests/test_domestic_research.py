"""Domestic search integration: all HTTP and credentials are isolated doubles."""
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from wot.connections import ConnectionManager
from wot.domain import DomainError
from wot.research import ResearchManager
from wot.storage import AppStore, digest

URL = "https://www.example.edu.cn/admissions/2026"
QUERY = "2026 山东 物理 本科 招生计划"
BODY = "2026年 山东省 物理类 本科批\n示例大学 专业组101 投档最低分590分，最低位次30000名。"
SNIPPET = "搜索摘要不是已读取正文：2026年示例大学投档590分。"
KEYS = {provider: "sk-isolated-domestic-" + provider + "-not-real" for provider in ("deepseek", "kimi", "glm")}


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, ref):
        return self.values.get((service, ref))

    def set_password(self, service, ref, value):
        self.values[service, ref] = value

    def delete_password(self, service, ref):
        self.values.pop((service, ref), None)


def answer(request, *, raw=True):
    host, path = request.url.host, request.url.path
    if host == "api.deepseek.com":
        if path.endswith("/fetch"):
            return httpx.Response(200, json={"title": "招生正文", "content": BODY})
        return httpx.Response(200, json={"results": [{"title": "招生计划", "url": URL,
            "content": BODY if raw else "", "summary": SNIPPET, "date": "2026-07-01T00:00:00Z"}]})
    if host == "api.moonshot.cn":
        if path.endswith("/fetch"):
            return httpx.Response(200, json={"title": "招生正文", "url": URL, "markdown": BODY})
        return httpx.Response(200, json={"search_results": [{"title": "招生计划", "url": URL,
            "text": BODY if raw else "", "snippet": SNIPPET, "date": "2026-07-01T00:00:00Z"}]})
    if host == "open.bigmodel.cn":
        if path.endswith("/reader"):
            return httpx.Response(200, json={"reader_result": {"title": "招生正文", "url": URL, "content": BODY}})
        return httpx.Response(200, json={"search_result": [{"title": "招生计划", "link": URL,
            "content": SNIPPET, "publish_date": "2026-07-01T00:00:00Z"}]})
    if host == "api.bocha.cn":
        return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": [{
            "name": "招生计划", "url": URL, "summary": SNIPPET, "datePublished": "2026-07-01"}]}}})
    if host == "api.tavily.com":
        if path.endswith("/extract"):
            return httpx.Response(200, json={"results": [{"url": URL, "raw_content": BODY}]})
        return httpx.Response(200, json={"results": [{"url": URL, "title": "招生计划"}]})
    raise AssertionError("unexpected network target")


@pytest.fixture
def rig(tmp_path):
    store = AppStore(tmp_path / "isolated.db")
    vault, calls, model_calls, handlers = Vault(), [], [], [None]

    def model_dispatch(request):
        model_calls.append(request)
        raise AssertionError("Search integration must never invoke a chat model")

    def dispatch(request):
        calls.append({"host": request.url.host, "path": request.url.path,
                      "authorization": request.headers.get("authorization"),
                      "body": json.loads(request.content)})
        if handlers[0]:
            return handlers[0](request)
        return answer(request)

    model_client = httpx.Client(transport=httpx.MockTransport(model_dispatch))
    connections = ConnectionManager(store, tmp_path, credential_backend=vault, http_client=model_client)
    saved = {provider: connections.save({"provider": provider, "model": "isolated-model",
        "api_key": key, "remember": True}) for provider, key in KEYS.items()}
    service = SimpleNamespace(store=store, lock=threading.RLock(), privacy_revision=0)
    client = httpx.Client(transport=httpx.MockTransport(dispatch))
    manager = ResearchManager(service, tmp_path, credential_backend=vault, http_client=client,
        connection_manager=connections,
        resolver=lambda host, port, **kwargs: [(2, 1, 6, "", ("9.9.9.9", port))])
    value = SimpleNamespace(store=store, vault=vault, manager=manager, connections=connections,
        saved=saved, calls=calls, model_calls=model_calls, handlers=handlers, service=service)
    try:
        yield value
    finally:
        manager.close()
        connections.close()
        client.close()
        model_client.close()
        store.db.close()


def configure(rig, provider="deepseek", **changes):
    body = {"provider": provider, "credential_mode": "CONNECTION",
            "connection_id": rig.saved[provider]["id"], **changes}
    result = rig.manager.validate(body)
    assert result["saved"], result
    rig.calls.clear()
    return result


def begin(rig, scope="room"):
    return rig.manager.begin(scope_kind="CHAT", scope_id=scope, agent_id="isolated-agent",
        generation_id="isolated-generation", version_id="voice-v1")


def collect(rig, provider="deepseek"):
    configure(rig, provider)
    rid = begin(rig)
    return rid, rig.manager.collect(rid, [QUERY])


@pytest.mark.parametrize("provider,host", [("deepseek", "api.deepseek.com"), ("kimi", "api.moonshot.cn"), ("glm", "open.bigmodel.cn")])
def test_reuse_saved_key_tests_search_only_and_never_copies_secret_to_web_config(rig, provider, host):
    result = rig.manager.validate({"provider": provider, "credential_mode": "CONNECTION",
        "connection_id": rig.saved[provider]["id"]})
    assert result["saved"] and result["tested"]
    assert result["config"]["credential_mode"] == "CONNECTION"
    assert result["config"]["storage"] == "OS_CREDENTIAL_STORE"
    assert result["config"]["api_key_mask"] == "••••••••"
    assert len(rig.calls) == 1 and rig.calls[0]["host"] == host
    assert rig.calls[0]["authorization"] == "Bearer " + KEYS[provider]
    assert not rig.model_calls and not rig.store.all("model_call")
    stored = rig.store.all("research_config")[0]
    assert stored["credential_ref"] is None and stored["connection_id"] == rig.saved[provider]["id"]
    assert not rig.manager._session_keys


@pytest.mark.parametrize("provider", ["deepseek", "kimi"])
def test_original_content_in_search_response_skips_fetch_and_uses_no_model_tokens(rig, provider):
    _, context = collect(rig, provider)
    assert context["status"] == "READY"
    assert len(rig.calls) == 1 and rig.calls[0]["path"].endswith("/search")
    source = context["sources"][0]
    assert source["excerpt"] == BODY and source["verified_body"] is True
    assert source["evidence_kind"] == "PAGE_CONTENT" and source["provider"] == provider
    ledger = rig.store.all("research_call")[-1]
    assert ledger["provider"] == provider and ledger["credits"] is None and ledger["fee"] is None
    assert not rig.store.all("model_call") and not rig.model_calls


@pytest.mark.parametrize("provider,path", [("deepseek", "/v1/tools/fetch"), ("kimi", "/v1/tools/fetch"), ("glm", "/api/paas/v4/reader")])
def test_missing_original_search_body_reads_same_provider_official_fetch(rig, provider, path):
    configure(rig, provider)
    rig.handlers[0] = lambda request: answer(request, raw=False)
    rid = begin(rig)
    result = rig.manager.collect(rid, [QUERY])
    assert result["status"] == "READY" and result["sources"][0]["excerpt"] == BODY
    assert [c["path"] for c in rig.calls][-1] == path and len(rig.calls) == 2
    assert {c["authorization"] for c in rig.calls} == {"Bearer " + KEYS[provider]}
    assert all(c["host"] != "api.tavily.com" for c in rig.calls)


def test_bocha_summary_is_explicitly_not_original_content_and_never_triggers_another_vendor(rig):
    assert rig.manager.validate({"provider": "bocha", "credential_mode": "SEPARATE",
        "api_key": "bocha-isolated-test-key", "remember_key": False})["saved"]
    rig.calls.clear()
    result = rig.manager.collect(begin(rig), [QUERY])
    assert result["status"] == "SNIPPETS_ONLY" and "摘要" in result["notice"]
    assert result["sources"][0]["evidence_kind"] == "SEARCH_SNIPPET"
    assert result["sources"][0]["verified_body"] is False
    assert len(rig.calls) == 1 and rig.calls[0]["host"] == "api.bocha.cn"


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500])
def test_failed_new_service_test_preserves_old_connection_key_and_web_config(rig, status):
    configure(rig)
    old = rig.manager.config()
    rig.handlers[0] = lambda request: httpx.Response(status, json={"error": {"message": KEYS["kimi"]}})
    result = rig.manager.validate({"provider": "kimi", "credential_mode": "CONNECTION",
        "connection_id": rig.saved["kimi"]["id"]})
    assert result["status"] == "TEST_FAILED" and not result["saved"]
    assert rig.manager.config() == old
    assert rig.connections.active()["key_status"] == "CONFIGURED"
    assert KEYS["kimi"] not in json.dumps(result)


@pytest.mark.parametrize("provider,key", [("tavily", KEYS["deepseek"]), ("deepseek", "tvly-isolated-wrong-vendor"), ("kimi", "tvly-isolated-wrong-vendor"), ("glm", "tvly-isolated-wrong-vendor")])
def test_wrong_vendor_key_is_explained_before_any_http_or_ledger_dispatch(rig, provider, key):
    with pytest.raises(DomainError, match="Key"):
        rig.manager.validate({"provider": provider, "credential_mode": "SEPARATE", "api_key": key})
    assert not rig.calls and not rig.store.all("research_call")


@pytest.mark.parametrize("provider,other", [("deepseek", "kimi"), ("kimi", "glm"), ("glm", "deepseek")])
def test_different_model_provider_key_cannot_be_reused_for_selected_search_service(rig, provider, other):
    with pytest.raises(DomainError, match="对应供应商"):
        rig.manager.validate({"provider": provider, "credential_mode": "CONNECTION",
            "connection_id": rig.saved[other]["id"]})
    assert not rig.calls


@pytest.mark.parametrize("base", ["https://proxy.example.com/v1", "http://api.deepseek.com/v1", "https://api.deepseek.com.evil.example/v1", "https://user:password@api.deepseek.com/v1", "https://api.deepseek.com/v1?token=not-a-key"])
def test_proxy_or_nonofficial_model_host_cannot_forward_its_key_to_original_provider(rig, base):
    row = rig.connections._get(rig.saved["deepseek"]["id"])
    with rig.store.transaction():
        row["base_url"] = base
        rig.store.update(row)
    with pytest.raises(DomainError, match="官方接口"):
        rig.manager.validate({"provider": "deepseek", "credential_mode": "CONNECTION", "connection_id": row["id"]})
    assert not rig.calls


def test_removed_model_connection_does_not_send_any_key_or_retain_configured_state(rig):
    configure(rig)
    rig.connections.remove(rig.saved["deepseek"]["id"])
    assert rig.manager.config()["configured"] is False
    result = rig.manager.collect(begin(rig), [QUERY])
    assert result["status"] == "NOT_CONFIGURED" and not rig.calls


def test_rotation_marks_old_research_stale_and_uses_new_key_only_after_explicit_new_run(rig):
    configure(rig)
    old_run = begin(rig)
    new_key = "sk-rotated-isolated-deepseek-never-real"
    rig.connections.save({"api_key": new_key}, rig.saved["deepseek"]["id"])
    assert rig.manager.stale(old_run)
    with pytest.raises(DomainError, match="取消"):
        rig.manager.collect(old_run, [QUERY])
    assert not rig.calls
    result = rig.manager.collect(begin(rig), [QUERY])
    assert result["status"] == "READY" and rig.calls[0]["authorization"] == "Bearer " + new_key


def test_key_rotation_during_response_discards_late_body_but_keeps_dispatched_usage_record(rig):
    configure(rig)
    rid = begin(rig)

    def rotate(request):
        rig.connections.save({"api_key": "sk-new-during-request-isolated"}, rig.saved["deepseek"]["id"])
        return answer(request)

    rig.handlers[0] = rotate
    with pytest.raises(DomainError, match="取消"):
        rig.manager.collect(rid, [QUERY])
    assert not rig.manager.context(rid)["sources"]
    ledger = rig.store.all("research_call")[-1]
    assert ledger["state"] == "COMPLETED" and ledger["discarded"] is True


def test_connection_change_during_validation_cannot_save_the_late_test_result(rig):
    def rotate(request):
        rig.connections.save({"api_key": "sk-rotation-during-validation-isolated"}, rig.saved["deepseek"]["id"])
        return answer(request)

    rig.handlers[0] = rotate
    result = rig.manager.validate({"provider": "deepseek", "credential_mode": "CONNECTION",
        "connection_id": rig.saved["deepseek"]["id"]})
    assert result["status"] == "SAVE_FAILED" and result["tested"] and not result["saved"]
    assert not rig.store.all("research_config")


@pytest.mark.parametrize("provider,length", [("deepseek", 129), ("glm", 71)])
def test_all_query_lengths_are_validated_before_any_paid_request_in_multiquery_round(rig, provider, length):
    configure(rig, provider)
    with pytest.raises(DomainError, match="缩短"):
        rig.manager.collect(begin(rig), [QUERY, "中" * length])
    assert not rig.calls


def test_private_search_material_never_persists_raw_query_or_secret_even_when_provider_echoes_it(rig):
    configure(rig)
    query = "仅本次公开检索词招生教育条件"
    rig.handlers[0] = lambda request: httpx.Response(200, json={"results": [{
        "url": URL, "title": KEYS["deepseek"], "content": "正文 " + KEYS["deepseek"] + " user@example.com"}]})
    result = rig.manager.collect(begin(rig), [query])
    durable = json.dumps({"runs": rig.store.all("research_run"), "calls": rig.store.all("research_call"),
                          "config": rig.store.all("research_config")}, ensure_ascii=False)
    assert query not in durable and KEYS["deepseek"] not in durable and "user@example.com" not in durable
    assert "凭据已隐藏" in result["sources"][0]["excerpt"]
    assert query not in json.dumps(rig.manager.config(), ensure_ascii=False)


def test_same_query_cache_never_crosses_provider_or_conversation_boundary(rig):
    configure(rig)
    first = begin(rig, "room-one")
    result = rig.manager.collect(first, [QUERY])
    with rig.store.transaction():
        room = rig.store.add("conversation", {"title": "测试", "skill_id": "none"}, rid="room-one")
        mid = rig.store.add_message(room["id"], "AGENT", "已发布参考。", request_key="published")
        rig.manager.publish(first, mid, 0, "已发布参考。")
    assert result["sources"]
    rig.calls.clear()
    rig.manager.collect(begin(rig, "room-one"), [QUERY])
    assert rig.calls == []
    rig.manager.collect(begin(rig, "room-two"), [QUERY])
    assert len(rig.calls) == 1
    configure(rig, "kimi")
    result = rig.manager.collect(begin(rig, "room-one"), [QUERY])
    assert len(rig.calls) == 1 and rig.calls[0]["host"] == "api.moonshot.cn"
    assert all(source["provider"] == "kimi" for source in result["sources"])
    assert all(source["cache_hit"] is False for source in result["sources"])


def test_fetch_redirect_to_private_or_unrelated_url_is_never_attached_as_read_source(rig):
    configure(rig, "kimi")

    def dispatch(request):
        if request.url.path.endswith("/fetch"):
            return httpx.Response(200, json={"url": "http://127.0.0.1/private", "title": "secret", "markdown": BODY})
        return answer(request, raw=False)

    rig.handlers[0] = dispatch
    result = rig.manager.collect(begin(rig), [QUERY])
    assert not result["sources"] and result["unread_sources"][0]["reason"] == "资料未读取"
    assert all(call["host"] == "api.moonshot.cn" for call in rig.calls)


def test_dispatch_callback_can_cancel_without_a_network_request(rig):
    configure(rig)
    rid = begin(rig)

    def cancel(request_id):
        raise DomainError("本次已取消", 409)

    with pytest.raises(DomainError, match="取消"):
        rig.manager.collect(rid, [QUERY], on_request=cancel)
    assert not rig.calls
    ledger = rig.store.all("research_call")[-1]
    assert ledger["state"] == "NOT_SENT"


def test_separate_key_stays_in_session_storage_and_remove_does_not_remove_model_key(rig):
    extra = "sk-separate-isolated-deepseek-not-real"
    result = rig.manager.validate({"provider": "deepseek", "credential_mode": "SEPARATE",
        "api_key": extra, "remember_key": False})
    assert result["saved"] and result["config"]["storage"] == "SESSION_ONLY"
    assert extra not in json.dumps(rig.store.all("research_config"))
    assert extra not in json.dumps(rig.manager.config())
    rig.manager.remove()
    assert rig.connections._key(rig.connections._get(rig.saved["deepseek"]["id"])) == KEYS["deepseek"]
    assert not rig.manager._session_keys


def test_request_hash_and_ledger_keep_provider_without_private_query_text(rig):
    configure(rig)
    query = "公开课程招生参考"
    rig.manager.collect(begin(rig), [query])
    record = rig.store.all("research_call")[-1]
    assert record["provider"] == "deepseek"
    assert record["connection_id"] == rig.saved["deepseek"]["id"]
    assert record["request_hash"] == digest(["deepseek", {"query": query, "count": 5, "need_content": True}])
    assert query not in json.dumps(record)
