import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from dive_memory.api import create_app
from dive_memory.auth import StaticTokenAuthorizer


def request(app, method: str, path: str, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def test_http_event_retrieve_and_idempotency():
    app = create_app(":memory:")
    assert request(app, "GET", "/healthz").json() == {"status": "ok", "storage": "sqlite"}

    body = {"namespace": "u1", "text": "请记住我喜欢咖啡", "explicit": True, "idempotency_key": "req-1"}
    first = request(app, "POST", "/v1/events", json=body)
    assert first.status_code == 200
    assert first.json()["memory_ids"]
    duplicate = request(app, "POST", "/v1/events", json=body)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["event_id"] == first.json()["event_id"]

    result = request(app, "POST", "/v1/retrieve", json={"namespace": "u1", "query": "咖啡"})
    assert result.status_code == 200
    assert result.json()["items"][0]["memory"]["content"] == "preference: 咖啡"


def test_http_deferred_job_context_and_delete_contracts():
    app = create_app(":memory:")
    created = request(app, "POST", "/v1/events", json={
        "namespace": "u1", "text": "请记住我喜欢绿茶", "explicit": True, "defer": True,
        "idempotency_key": "deferred-1",
    })
    assert created.status_code == 200
    assert created.json()["memory_ids"] == []
    assert request(app, "POST", "/v1/retrieve", json={"namespace": "u1", "query": "绿茶"}).json()["items"] == []

    processed = request(app, "POST", "/v1/jobs/process-outbox", json={})
    assert processed.status_code == 200
    assert processed.json()["processed"] == 1
    event_id = created.json()["event_id"]
    memory_id = processed.json()["results"][0]["memory_ids"][0]

    context = request(app, "POST", "/v1/context", json={"namespace": "u1", "query": "绿茶", "token_budget": 20})
    assert context.status_code == 200
    assert context.json()["estimated_tokens"] <= 20
    deleted = request(app, "POST", f"/v1/events/{event_id}/forget")
    assert deleted.status_code == 200
    assert memory_id in deleted.json()["deleted_memory_ids"]
    assert request(app, "GET", f"/v1/memories/{memory_id}").json()["status"] == "DELETED"


def test_http_validation_and_missing_resource_errors():
    app = create_app(":memory:")
    assert request(app, "POST", "/v1/events", json={"namespace": "u1"}).status_code == 400
    assert request(app, "POST", "/v1/events", json={
        "namespace": "u1", "text": "请记住我喜欢咖啡", "explicit": "false",
    }).status_code == 400
    assert request(app, "POST", "/v1/context", json={"namespace": "u1"}).status_code == 400
    assert request(app, "POST", "/v1/memory-mode", json={}).status_code == 400
    assert request(app, "POST", "/v1/memory-mode", json={"namespace": "u1", "enabled": "false"}).status_code == 400
    assert request(app, "POST", "/v1/retrieve", json={"namespace": "u1", "query": "咖啡", "limit": "many"}).status_code == 400
    assert request(app, "POST", "/v1/events/missing/forget").status_code == 404


def test_http_rejects_unparsable_timestamps_and_returns_full_memory_payload():
    app = create_app(":memory:")
    request(app, "POST", "/v1/events", json={
        "namespace": "u1", "text": "请记住我喜欢咖啡", "explicit": True,
        "idempotency_key": "payload-1",
    })

    # A malformed as_of/now used to be ignored, which answers a different
    # question than the caller asked.
    assert request(app, "POST", "/v1/retrieve", json={
        "namespace": "u1", "query": "咖啡", "as_of": "someday"}).status_code == 400
    assert request(app, "POST", "/v1/jobs/decay", json={
        "namespace": "u1", "now": "someday"}).status_code == 400
    assert request(app, "GET", "/v1/users/u1/timeline?as_of=someday").status_code == 400

    # Memory has slots=True, so the payload must not depend on __dict__.
    item = request(app, "POST", "/v1/retrieve", json={"namespace": "u1", "query": "咖啡"}).json()["items"][0]
    assert item["memory"]["status"] == "ACTIVE"
    assert item["memory"]["observed_at"]
    assert item["memory"]["durability"] == "permanent"
    assert "vector" not in item["memory"]

    exhausted = request(app, "POST", "/v1/retrieve", json={
        "namespace": "u1", "query": "咖啡", "token_budget": 0,
    }).json()
    assert exhausted["items"] == []
    assert exhausted["budget_used"] == 0
    assert exhausted["warnings"]
    assert exhausted["abstain_reason"] == "token budget exhausted"


def test_http_uses_strict_schemas_and_async_default():
    app = create_app(":memory:")

    assert request(app, "POST", "/v1/events", json={
        "namespace": "u1", "text": "我住在上海", "idempotency_key": "bad-time",
        "observed_at": "not-a-time",
    }).status_code == 400
    assert request(app, "POST", "/v1/retrieve", json={
        "namespace": "u1", "query": "上海", "limit": 1.9,
    }).status_code == 400
    assert request(app, "POST", "/v1/events", json={
        "namespace": "u1", "text": "我住在上海",
    }).status_code == 400

    # Ordinary events are appended quickly and projected by the worker. An
    # explicit remember request remains available as the immediate path.
    deferred = request(app, "POST", "/v1/events", json={
        "namespace": "u1", "text": "我住在上海", "idempotency_key": "async-default",
    })
    assert deferred.status_code == 200
    assert deferred.json()["deferred"] is True
    assert deferred.json()["memory_ids"] == []
    assert request(app, "POST", "/v1/jobs/process-outbox", json={}).json()["processed"] == 1


def test_http_planned_candidate_search_reindex_and_scoped_forget_routes():
    app = create_app(":memory:")
    candidate = request(app, "POST", "/v1/memories/candidates", json={
        "namespace": "u1", "text": "我喜欢绿茶", "idempotency_key": "candidate-1",
    })
    assert candidate.status_code == 200
    assert candidate.json()["deferred"] is True
    processed = request(app, "POST", "/v1/jobs/process-outbox", json={}).json()
    memory_id = processed["results"][0]["memory_ids"][0]

    search = request(app, "POST", "/v1/search", json={"namespace": "u1", "query": "绿茶"})
    assert search.status_code == 200
    assert search.json()["items"][0]["memory"]["id"] == memory_id
    assert "query_plan" in search.json()
    assert "budget_used" in search.json()

    correction = request(app, "PATCH", f"/v1/memories/{memory_id}", json={
        "content": "我喜欢咖啡", "idempotency_key": "correction-1",
    })
    assert correction.status_code == 200
    corrected_id = correction.json()["memory_ids"][0]
    versioned = request(app, "GET", f"/v1/memories/{corrected_id}?include_versions=true")
    assert versioned.status_code == 200
    assert versioned.json()["version"] == 2
    assert [(item["version"], item["reason"]) for item in versioned.json()["versions"]] == [
        (1, "created"), (2, "supersession"),
    ]

    rebuilt = request(app, "POST", "/v1/jobs/reindex", json={
        "namespace": "u1", "indexes": ["projections"],
    })
    assert rebuilt.status_code == 200
    assert rebuilt.json()["counts"]["events"] == 2

    forgotten = request(app, "POST", "/v1/users/u1/forget", json={"topic": "咖啡", "hard": True})
    assert forgotten.status_code == 200
    assert forgotten.json()["deleted_event_ids"]
    assert request(app, "GET", f"/v1/memories/{corrected_id}").status_code == 404


def test_http_optional_authorizer_enforces_namespace_boundary():
    app = create_app(":memory:", authorizer=StaticTokenAuthorizer({
        "token-u1": {"u1"},
        "admin": {"*"},
    }))
    body = {
        "namespace": "u1", "text": "请记住我喜欢绿茶", "explicit": True,
        "idempotency_key": "auth-1",
    }
    assert request(app, "POST", "/v1/events", json=body).status_code == 401
    assert request(app, "POST", "/v1/events", json=body,
                   headers={"Authorization": "Bearer wrong"}).status_code == 403
    created = request(app, "POST", "/v1/events", json=body,
                      headers={"Authorization": "Bearer token-u1"})
    assert created.status_code == 200
    assert request(app, "POST", "/v1/retrieve", json={"namespace": "u2", "query": "绿茶"},
                   headers={"Authorization": "Bearer token-u1"}).status_code == 403
    assert request(app, "POST", "/v1/jobs/process-outbox", json={},
                   headers={"Authorization": "Bearer token-u1"}).status_code == 403
    assert request(app, "POST", "/v1/jobs/process-outbox", json={},
                   headers={"Authorization": "Bearer admin"}).status_code == 200


def test_http_rejects_invalid_query_bounds_and_mixed_projection_reindex():
    app = create_app(":memory:")
    assert request(app, "GET", "/v1/users/u1/memories?limit=0").status_code == 400
    assert request(app, "GET", "/v1/users/u1/memories?offset=-1").status_code == 400
    assert request(app, "GET", "/v1/jobs?limit=1001").status_code == 400
    response = request(app, "POST", "/v1/jobs/reindex", json={
        "indexes": ["projections", "vector"],
    })
    assert response.status_code == 400
    assert "by itself" in response.json()["detail"]
    assert request(app, "POST", "/v1/jobs/reindex", json={"indexes": []}).status_code == 400
