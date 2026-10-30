"""Provider contracts use in-process HTTP only; no real credentials or calls."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from wot.connections import ConnectionManager
from wot.domain import DomainError
from wot.storage import AppStore

SECRET = "test-secret-never-persist-7bb1"
MESSAGES = [
    {"role": "system", "content": "safe instruction"},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "previous"},
    {"role": "user", "content": "next"},
]


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, ref):
        return self.values.get((service, ref))

    def set_password(self, service, ref, value):
        self.values[service, ref] = value

    def delete_password(self, service, ref):
        self.values.pop((service, ref), None)


class FailedVault(Vault):
    def set_password(self, service, ref, value):
        raise RuntimeError(SECRET)


def make_manager(tmp_path, handler, vault=None):
    store = AppStore(tmp_path / "test.db")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ConnectionManager(
        store, tmp_path, credential_backend=vault or Vault(), http_client=client
    )


def response_for(protocol):
    if protocol == "messages":
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 3},
        }
    if protocol == "generateContent":
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "private reasoning", "thought": True}, {"text": "ok"}]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 3,
                "totalTokenCount": 18,
            },
        }
    if protocol == "responses":
        return {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "status": "completed",
            "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
        }
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


@pytest.mark.parametrize(
    "provider,protocol,path,auth",
    [
        ("openai", "responses", "/v1/responses", "authorization"),
        ("claude", "messages", "/v1/messages", "x-api-key"),
        (
            "gemini",
            "generateContent",
            "/v1beta/models/test-model:generateContent",
            "x-goog-api-key",
        ),
        ("deepseek", "chat", "/v1/chat/completions", "authorization"),
        ("qwen", "chat", "/compatible-mode/v1/chat/completions", "authorization"),
        ("glm", "chat", "/api/paas/v4/chat/completions", "authorization"),
        ("kimi", "chat", "/v1/chat/completions", "authorization"),
        ("doubao", "responses", "/api/v3/responses", "authorization"),
        ("custom", "chat", "/v1/chat/completions", "authorization"),
    ],
)
def test_provider_request_response_usage_and_ledger(tmp_path, provider, protocol, path, auth):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_for(protocol))

    manager = make_manager(tmp_path, handler)
    data = {"provider": provider, "model": "test-model", "api_key": SECRET, "remember": False}
    if provider == "custom":
        data["base_url"] = "http://127.0.0.1:9000/v1"
    saved = manager.save(data)
    assert len(requests) == 0  # saving never makes a paid call
    result = manager.complete(MESSAGES, max_tokens=93, purpose="assessment_prediction")
    request = requests[0]
    assert request.url.path == path
    assert SECRET in request.headers[auth]
    assert SECRET not in str(request.url)
    body = json.loads(request.content)
    if protocol == "messages":
        assert body["system"] == "safe instruction"
        assert body["messages"][0] == MESSAGES[1]
        assert body["max_tokens"] == 93
        assert request.headers["anthropic-version"] == "2023-06-01"
    elif protocol == "generateContent":
        assert body["systemInstruction"]["parts"][0]["text"] == "safe instruction"
        assert body["contents"][1]["role"] == "model"
        assert body["generationConfig"]["maxOutputTokens"] == 93
    elif protocol == "responses":
        assert body["input"] == MESSAGES
        assert body["max_output_tokens"] == 93
        assert body["store"] is False
    else:
        assert body["messages"] == MESSAGES
        assert body["max_tokens"] == 93
    assert result["text"] == "ok"
    assert result["classification"] == "LIVE"
    assert result["input_tokens"] == 11 and result["output_tokens"] == 3
    assert result["tokens"] == (18 if protocol == "generateContent" else 14)
    assert result["connection_id"] == saved["id"]
    assert result["finish_reason"]
    assert manager.external_calls == 1
    assert manager.calls == []  # no raw live prompt capture
    ledger = manager.store.all("model_call")
    assert ledger[0]["purpose"] == "assessment_prediction"
    assert ledger[0]["status"] == "SUCCESS"
    durable = json.dumps(manager.store.all("model_connection") + ledger)
    assert SECRET not in durable and "safe instruction" not in durable and "hello" not in durable
    assert SECRET.encode() not in (tmp_path / "test.db").read_bytes()
    assert "credential_ref" not in saved


def test_secure_storage_session_restart_and_rotation_hash(tmp_path):
    vault = Vault()
    manager = make_manager(
        tmp_path, lambda _: httpx.Response(200, json=response_for("chat")), vault
    )
    first = manager.save(
        {"provider": "deepseek", "model": "test", "api_key": SECRET, "remember": True}
    )
    fingerprint = manager.config_hash()
    assert vault.values and first["storage"] == "OS_CREDENTIAL_STORE"
    manager.save({"api_key": "rotated-key", "label": "新名字"}, first["id"])
    assert manager.config_hash() == fingerprint
    restarted = ConnectionManager(
        manager.store, tmp_path, credential_backend=vault, http_client=manager.client
    )
    assert restarted.is_live() is True
    manager.save({"remember": False}, first["id"])
    assert vault.values == {}
    assert manager.is_live()
    assert restarted.is_live() is False
    third = ConnectionManager(
        manager.store, tmp_path, credential_backend=vault, http_client=manager.client
    )
    assert third.list_connections()[0]["key_status"] == "MISSING"
    with pytest.raises(DomainError, match="Key"):
        third.complete(MESSAGES)


def test_no_plaintext_fallback_and_explicit_session_choice(tmp_path):
    manager = make_manager(tmp_path, lambda _: httpx.Response(500), FailedVault())
    with pytest.raises(DomainError, match="仅在本次运行") as failure:
        manager.save({"provider": "kimi", "model": "test", "api_key": SECRET, "remember": True})
    assert SECRET not in str(failure.value)
    assert manager.list_connections() == []
    manager.save({"provider": "kimi", "model": "test", "api_key": SECRET, "remember": False})
    assert manager.is_live()


def test_connection_management_and_test_generation_are_separate(tmp_path):
    manager = make_manager(tmp_path, lambda _: httpx.Response(200, json=response_for("chat")))
    a = manager.save({"provider": "kimi", "model": "one", "api_key": SECRET})
    b = manager.save({"provider": "kimi", "model": "two", "api_key": "other"})
    assert manager.active()["id"] == a["id"]
    manager.set_default(b["id"])
    assert manager.model == "two"
    result = manager.test(a["id"])
    assert result["ok"]
    manager.complete(MESSAGES)
    latest = {c["id"]: c for c in manager.list_connections()}
    assert latest[a["id"]]["last_test"]["status"] == "SUCCESS"
    assert latest[a["id"]]["last_generation"] is None
    assert latest[b["id"]]["last_generation"]["status"] == "SUCCESS"
    manager.remove(b["id"])
    assert manager.active()["id"] == a["id"]
    with pytest.raises(DomainError, match="已移除"):
        manager.complete(MESSAGES, connection_id=b["id"])


@pytest.mark.parametrize(
    "provider", ["openai", "claude", "gemini", "deepseek", "qwen", "glm", "kimi", "doubao"]
)
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 302])
def test_each_provider_errors_are_sanitized_no_retry(tmp_path, provider, status):
    hits = []

    def handler(request):
        hits.append(request)
        return httpx.Response(
            status,
            text=f"internal auth error {SECRET}",
            headers={"location": "https://other.example/"},
        )

    manager = make_manager(tmp_path, handler)
    manager.save({"provider": provider, "model": "test", "api_key": SECRET})
    with pytest.raises(DomainError) as failure:
        manager.complete(MESSAGES)
    assert len(hits) == 1
    assert SECRET not in str(failure.value)
    ledger = manager.store.all("model_call")
    assert len(ledger) == 1 and ledger[0]["status"] == f"HTTP_{status}"
    assert SECRET not in json.dumps(ledger)


@pytest.mark.parametrize(
    "payload", [{}, {"choices": []}, [], {"choices": [{"message": {"content": "ok"}}]}]
)
def test_malformed_or_unmetered_response_cannot_be_used(tmp_path, payload):
    manager = make_manager(tmp_path, lambda _: httpx.Response(200, json=payload))
    manager.save({"provider": "kimi", "model": "test", "api_key": SECRET})
    with pytest.raises(DomainError):
        manager.complete(MESSAGES)
    assert manager.store.all("model_call")[0]["status"] == "INVALID_RESPONSE"


def test_network_error_discards_exception_secret(tmp_path):
    def handler(request):
        raise httpx.ConnectError(SECRET, request=request)

    manager = make_manager(tmp_path, handler)
    manager.save({"provider": "kimi", "model": "test", "api_key": SECRET})
    with pytest.raises(DomainError) as failure:
        manager.complete(MESSAGES)
    assert SECRET not in str(failure.value)
    assert failure.value.__suppress_context__
    assert manager.store.all("model_call")[0]["status"] == "NETWORK_ERROR"


def test_qwen_regions_workspace_and_model_listing(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    manager = make_manager(tmp_path, handler)
    c = manager.save(
        {"provider": "qwen", "model": "test", "api_key": SECRET, "region": "international"}
    )
    assert c["base_url"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    c = manager.save({"region": "workspace", "workspace_id": "ws-123"}, c["id"])
    assert c["base_url"] == "https://ws-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    c = manager.save({"workspace_id": "ws-456"}, c["id"])
    assert "ws-456" in c["base_url"]
    assert manager.models(c["id"]) == [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}]
    assert SECRET not in str(requests[0].url)


def test_gemini_model_listing_and_manual_fallback(tmp_path):
    manager = make_manager(
        tmp_path,
        lambda _: httpx.Response(
            200, json={"models": [{"name": "models/gemini-test", "displayName": "Gemini Test"}]}
        ),
    )
    c = manager.save({"provider": "gemini", "model": "models/gemini-test", "api_key": SECRET})
    assert manager.models(c["id"]) == [{"id": "gemini-test", "name": "Gemini Test"}]
    manager.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    assert manager.models(c["id"]) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://external.example/v1",
        "https://user:secret@example.com/v1",
        "https://example.com/v1?key=secret",
        "file:///x",
        "https://example.com/#key",
    ],
)
def test_endpoint_validation(tmp_path, url):
    manager = make_manager(tmp_path, lambda _: httpx.Response(200))
    with pytest.raises(DomainError):
        manager.save({"provider": "custom", "model": "x", "api_key": SECRET, "base_url": url})
    assert manager.list_connections() == []


def test_global_limit_two_and_offline_has_no_assessment_unlock(tmp_path):
    count = 0
    peak = 0
    lock = threading.Lock()

    def handler(request):
        nonlocal count, peak
        with lock:
            count += 1
            peak = max(peak, count)
        time.sleep(0.03)
        with lock:
            count -= 1
        return httpx.Response(200, json=response_for("chat"))

    manager = make_manager(tmp_path, handler)
    assert manager.name == "mock"
    assert not manager.is_live()
    with pytest.raises(DomainError, match="配置"):
        manager.complete(MESSAGES, purpose="assessment_prediction")
    manager.save({"provider": "kimi", "model": "test", "api_key": SECRET})
    with ThreadPoolExecutor(max_workers=6) as pool:
        outputs = list(pool.map(lambda _: manager.complete(MESSAGES), range(6)))
    assert len(outputs) == 6 and peak == 2


def test_external_context_boundary(tmp_path):
    manager = make_manager(tmp_path, lambda _: httpx.Response(200))
    with pytest.raises(DomainError, match="私人"):
        manager.generate(
            role="EXTERNAL",
            topic="x",
            transcript=[],
            private=[{"text": "private"}],
            skill={},
            goals=[],
        )
    result = manager.generate(
        role="EXTERNAL", topic="x", transcript=[], private=[], skill={}, goals=[]
    )
    assert result["provider"] == "mock"
    assert manager.external_calls == 0


def test_anthropic_cache_tokens_count_toward_total_budget(tmp_path):
    response = response_for("messages")
    response["usage"].update(cache_creation_input_tokens=17, cache_read_input_tokens=19)
    manager = make_manager(tmp_path, lambda _: httpx.Response(200, json=response))
    manager.save({"provider": "claude", "model": "test", "api_key": SECRET})
    result = manager.complete(MESSAGES)
    assert result["input_tokens"] == 47
    assert result["tokens"] == 50
    assert manager.store.all("model_call")[0]["tokens"] == 50


def test_success_body_cannot_echo_key_into_chat_or_ledger(tmp_path):
    response = response_for("chat")
    response["choices"][0]["message"]["content"] = "Provider unexpectedly echoed " + SECRET
    response["choices"][0]["finish_reason"] = SECRET
    manager = make_manager(tmp_path, lambda _: httpx.Response(200, json=response))
    manager.save({"provider": "kimi", "model": "test", "api_key": SECRET})
    result = manager.complete(MESSAGES)
    assert SECRET not in json.dumps(result)
    assert result["finish_reason"] == "unknown"
    assert SECRET not in json.dumps(manager.store.all("model_call"))


def test_cancelled_queued_call_never_reaches_provider(tmp_path):
    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    hits = []

    def handler(request):
        hits.append(request)
        if len(hits) == 2:
            started.set()
        assert release.wait(3)
        return httpx.Response(200, json=response_for("chat"))

    manager = make_manager(tmp_path, handler)
    manager.save({"provider": "kimi", "model": "test", "api_key": SECRET})
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(manager.complete, MESSAGES)
            second = pool.submit(manager.complete, MESSAGES)
            assert started.wait(2)
            third = pool.submit(manager.complete, MESSAGES, cancelled=cancelled.is_set)
            cancelled.set()
            with pytest.raises(DomainError, match="取消"):
                third.result(timeout=2)
            assert len(hits) == 2
            release.set()
            assert first.result(timeout=2)["text"] == "ok"
            assert second.result(timeout=2)["text"] == "ok"
    finally:
        release.set()
    assert manager.external_calls == 2
    assert len(manager.store.all("model_call")) == 2


@pytest.mark.parametrize(
    "provider,auth",
    [
        ("openai", "authorization"),
        ("claude", "x-api-key"),
        ("gemini", "x-goog-api-key"),
        ("deepseek", "authorization"),
        ("qwen", "authorization"),
        ("glm", "authorization"),
        ("kimi", "authorization"),
        ("doubao", "authorization"),
    ],
)
def test_discover_models_without_model_or_saved_connection(tmp_path, provider, auth):
    requests = []
    vault = Vault()

    def handler(request):
        requests.append(request)
        payload = (
            {"models": [{"name": "models/available-test", "displayName": "Available model"}]}
            if provider == "gemini"
            else {"data": [{"id": "available-test", "display_name": "Available model"}]}
        )
        return httpx.Response(200, json=payload)

    manager = make_manager(tmp_path, handler, vault)
    before = manager.store.fetchall("SELECT * FROM resources")
    discovered = manager.discover({"provider": provider, "api_key": SECRET, "remember": True})
    assert discovered == [{"id": "available-test", "name": "Available model"}]
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path.endswith("/models")
    assert SECRET in requests[0].headers[auth]
    assert SECRET not in str(requests[0].url)
    if provider == "claude":
        assert requests[0].headers["anthropic-version"] == "2023-06-01"
    assert manager.active() is None and manager.list_connections() == []
    assert manager.store.fetchall("SELECT * FROM resources") == before
    assert manager.store.all("model_call") == []
    assert manager.store.fetchall("SELECT * FROM requests") == []
    assert manager._session_keys == {} and vault.values == {}
    assert manager.external_calls == 0 and manager.calls == []
    assert SECRET.encode() not in (tmp_path / "test.db").read_bytes()


def test_discover_does_not_replace_existing_default_or_credentials(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    manager = make_manager(tmp_path, handler)
    saved = manager.save({"provider": "kimi", "model": "existing-model", "api_key": SECRET})
    before = manager.list_connections()
    session_keys = dict(manager._session_keys)
    discovered = manager.discover(
        {
            "provider": "qwen",
            "api_key": "transient-not-a-real-key",
            "region": "workspace",
            "workspace_id": "ws-discovery",
        }
    )
    assert discovered == [{"id": "other-model", "name": "other-model"}]
    assert requests[0].url.host == "ws-discovery.cn-beijing.maas.aliyuncs.com"
    assert manager.active()["id"] == saved["id"]
    assert manager.list_connections() == before and manager._session_keys == session_keys
    assert "transient-not-a-real-key" not in repr(manager.store.all("model_connection"))


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_discovery_failure_is_sanitized_without_persisting_draft(tmp_path, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, text="echo " + SECRET, headers={"location": "https://untrusted.example/"}
        )

    manager = make_manager(tmp_path, handler)
    with pytest.raises(DomainError) as failure:
        manager.discover({"provider": "openai", "api_key": SECRET})
    assert SECRET not in str(failure.value)
    assert len(requests) == 1 and manager.list_connections() == []
    assert manager.store.all("model_call") == [] and manager.external_calls == 0


def test_discovery_custom_endpoint_manual_fallback_and_success_echo_guard(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": SECRET, "display_name": "echoed credential"},
                    {"id": "available", "display_name": "Name echoes " + SECRET},
                ]
            },
        )

    manager = make_manager(tmp_path, handler)
    assert manager.discover(
        {"provider": "custom", "api_key": SECRET, "base_url": "http://127.0.0.1:9989/v1"}
    ) == [{"id": "available", "name": "Name echoes [凭据已隐藏]"}]
    assert requests[0].url == "http://127.0.0.1:9989/v1/models"
    manager.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    assert manager.discover({"provider": "doubao", "api_key": SECRET}) == []


@pytest.mark.parametrize(
    "data",
    [
        {"provider": "unknown", "api_key": SECRET},
        {"provider": "openai"},
        {"provider": "openai", "api_key": " "},
        {"provider": "openai", "api_key": SECRET, "base_url": "https://u:p@example.com/v1"},
        {"provider": "openai", "api_key": SECRET, "base_url": "https://example.com/v1?key=leak"},
        {"provider": "openai", "api_key": SECRET, "protocol": "tools"},
    ],
)
def test_discovery_validation_happens_before_network_or_storage(tmp_path, data):
    requests = []
    manager = make_manager(tmp_path, lambda request: requests.append(request))
    with pytest.raises(DomainError):
        manager.discover(data)
    assert requests == [] and manager.list_connections() == []
