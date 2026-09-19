from __future__ import annotations

from typing import Any

from .service import MemoryService


def create_app(db_path: str = "memory.db") -> Any:
    """Create the optional FastAPI adapter.

    The domain/service layer deliberately does not require FastAPI. Install
    ``dive-memory[api]`` when exposing the HTTP service.
    """
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Install dive-memory[api] to use the HTTP adapter") from exc

    app = FastAPI(title="D.I.V.E. Memory API", version="0.1.0")
    service = MemoryService(db_path)

    @app.post("/v1/events")
    def ingest(body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("namespace") or not body.get("text"):
            raise HTTPException(400, "namespace and text are required")
        return service.ingest(body["namespace"], body["text"], event_type=body.get("event_type", "message"),
                              explicit=bool(body.get("explicit")), observed_at=body.get("observed_at"),
                              idempotency_key=body.get("idempotency_key"))

    @app.post("/v1/jobs/process-outbox")
    def process_outbox(body: dict[str, Any] | None = None) -> dict[str, Any]:
        results = service.process_pending(int((body or {}).get("limit", 100)))
        return {"processed": len(results), "results": results}

    @app.post("/v1/retrieve")
    def retrieve(body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("namespace") or not body.get("query"):
            raise HTTPException(400, "namespace and query are required")
        result = service.retrieve(body["namespace"], body["query"], limit=int(body.get("limit", 8)),
                                  as_of=body.get("as_of"), intent=body.get("intent"))
        return {"items": [{"memory": item.memory.__dict__ if hasattr(item.memory, "__dict__") else {
            "id": item.memory.id, "kind": item.memory.kind, "content": item.memory.content,
            "evidence_state": item.memory.evidence_state.value, "valid_from": item.memory.valid_from,
            "valid_to": item.memory.valid_to, "confidence": item.memory.confidence,
        }, "score": item.score, "channels": item.channels, "source_refs": item.source_refs} for item in result.items],
                "plan": result.plan, "abstain_reason": result.abstain_reason, "degraded": result.degraded}

    @app.post("/v1/context")
    def context(body: dict[str, Any]) -> dict[str, Any]:
        result = service.retrieve_context(body["namespace"], body["query"], limit=int(body.get("limit", 8)),
                                          token_budget=int(body.get("token_budget", 1500)), as_of=body.get("as_of"))
        return {"context": result["context"], "estimated_tokens": result["estimated_tokens"],
                "omitted": result["omitted"], "plan": result["plan"], "abstain_reason": result["abstain_reason"]}

    @app.get("/v1/memories/{memory_id}")
    def get_memory(memory_id: str) -> dict[str, Any]:
        memory = service.get_memory(memory_id)
        if memory is None:
            raise HTTPException(404, "memory not found")
        return {"id": memory.id, "namespace": memory.namespace, "kind": memory.kind,
                "content": memory.content, "status": memory.status.value,
                "evidence_state": memory.evidence_state.value, "source_event_ids": memory.source_event_ids}

    @app.patch("/v1/memories/{memory_id}")
    def correct_memory(memory_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("content"):
            raise HTTPException(400, "content is required")
        try:
            return service.correct_memory(memory_id, body["content"])
        except KeyError:
            raise HTTPException(404, "memory not found")

    @app.get("/v1/users/{namespace}/memories")
    def list_memories(namespace: str, status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return {"items": [{"id": memory.id, "kind": memory.kind, "content": memory.content,
                           "status": memory.status.value, "valid_from": memory.valid_from,
                           "valid_to": memory.valid_to, "source_event_ids": memory.source_event_ids}
                          for memory in service.list_memories(namespace, status=status, limit=limit, offset=offset)]}

    @app.get("/v1/users/{namespace}/profile")
    def profile(namespace: str) -> dict[str, Any]:
        return service.profile(namespace)

    @app.delete("/v1/memories/{memory_id}")
    def forget(memory_id: str) -> dict[str, Any]:
        if service.get_memory(memory_id) is None:
            raise HTTPException(404, "memory not found")
        service.forget(memory_id)
        return {"deleted": True, "memory_id": memory_id}

    @app.post("/v1/events/{event_id}/forget")
    def forget_event(event_id: str) -> dict[str, Any]:
        return {"deleted_memory_ids": service.forget_event(event_id)}

    @app.post("/v1/memory-mode")
    def memory_mode(body: dict[str, Any]) -> dict[str, Any]:
        service.set_memory_enabled(body["namespace"], bool(body.get("enabled", True)))
        return {"namespace": body["namespace"], "enabled": bool(body.get("enabled", True))}

    @app.get("/v1/users/{namespace}/timeline")
    def timeline(namespace: str, as_of: str | None = None) -> dict[str, Any]:
        return {"items": [{"id": m.id, "kind": m.kind, "content": m.content, "status": m.status.value,
                           "valid_from": m.valid_from, "valid_to": m.valid_to, "source_event_ids": m.source_event_ids}
                          for m in service.timeline(namespace, as_of=as_of)]}

    @app.get("/v1/export")
    def export_namespace(namespace: str) -> dict[str, Any]:
        return service.export_namespace(namespace)

    @app.post("/v1/jobs/consolidate")
    def run_consolidation(body: dict[str, Any]) -> dict[str, Any]:
        report = service.consolidate(body["namespace"], dry_run=bool(body.get("dry_run", True)))
        return {"namespace": report.namespace, "examined": report.examined, "merged": report.merged,
                "archived": report.archived, "dry_run": report.dry_run}

    @app.post("/v1/jobs/decay")
    def decay(body: dict[str, Any]) -> dict[str, Any]:
        return service.decay(body["namespace"], now=body.get("now"), dry_run=bool(body.get("dry_run", True)))

    return app
