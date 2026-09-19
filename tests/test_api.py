import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from dive_memory.api import create_app


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
    assert request(app, "POST", "/v1/context", json={"namespace": "u1"}).status_code == 400
    assert request(app, "POST", "/v1/memory-mode", json={}).status_code == 400
    assert request(app, "POST", "/v1/events/missing/forget").status_code == 404
