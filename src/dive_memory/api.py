from typing import Any

from .auth import NamespaceAuthorizer
from .context import pack_context
from .models import Memory
from .service import MemoryService


def memory_payload(memory: Memory) -> dict[str, Any]:
    """Serialise a memory for an HTTP response.

    ``Memory`` uses ``slots=True``, so instances have no ``__dict__``. The old
    ``item.memory.__dict__ if hasattr(...) else {...}`` branch was dead code, and
    the fallback left out fields such as ``status`` and ``observed_at``. Being
    explicit keeps the payload stable and keeps internal vectors out of it.
    """
    return {
        "id": memory.id,
        "namespace": memory.namespace,
        "kind": memory.kind,
        "content": memory.content,
        "structured_content": memory.structured_content,
        "status": memory.status.value,
        "evidence_state": memory.evidence_state.value,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "salience": memory.salience,
        "durability": memory.durability,
        "valid_from": memory.valid_from,
        "valid_to": memory.valid_to,
        "observed_at": memory.observed_at,
        "version": memory.version,
        "supersedes_id": memory.supersedes_id,
        "source_event_ids": memory.source_event_ids,
        "model_version": memory.model_version,
        "extractor_version": memory.extractor_version,
    }


def create_app(db_path: str = "memory.db", *, extractor: Any = None, embedder: Any = None,
               authorizer: NamespaceAuthorizer | None = None) -> Any:
    """Create the optional FastAPI adapter.

    The domain/service layer deliberately does not require FastAPI. Install
    ``dive-memory[api]`` when exposing the HTTP service.
    """
    try:
        from fastapi import FastAPI, Header, HTTPException, Query
        from fastapi.encoders import jsonable_encoder
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, ConfigDict, Field
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Install dive-memory[api] to use the HTTP adapter") from exc

    class RequestModel(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    class EventRequest(RequestModel):
        namespace: str = Field(min_length=1)
        text: str = Field(min_length=1)
        event_type: str = Field(default="message", min_length=1)
        explicit: bool = Field(default=False, strict=True)
        observed_at: str | None = None
        occurred_from: str | None = None
        occurred_to: str | None = None
        source_message_id: str | None = None
        idempotency_key: str = Field(min_length=1)
        defer: bool | None = Field(default=None, strict=True)

    class ProcessOutboxRequest(RequestModel):
        limit: int = Field(default=100, ge=1, le=1000, strict=True)

    class CandidateRequest(RequestModel):
        namespace: str = Field(min_length=1)
        text: str = Field(min_length=1)
        observed_at: str | None = None
        idempotency_key: str = Field(min_length=1)
        defer: bool = Field(default=True, strict=True)

    class RetrieveRequest(RequestModel):
        namespace: str = Field(min_length=1)
        query: str = Field(min_length=1)
        limit: int = Field(default=8, ge=1, le=100, strict=True)
        token_budget: int = Field(default=1500, ge=0, le=100_000, strict=True)
        as_of: str | None = None
        observed_as_of: str | None = None
        intent: str | None = None

    class ContextRequest(RetrieveRequest):
        pass

    class CorrectionRequest(RequestModel):
        content: str = Field(min_length=1)
        namespace: str | None = Field(default=None, min_length=1)
        idempotency_key: str = Field(min_length=1)

    class MemoryModeRequest(RequestModel):
        namespace: str = Field(min_length=1)
        enabled: bool = Field(strict=True)

    class ReinforceRequest(RequestModel):
        namespace: str | None = Field(default=None, min_length=1)

    class NamespaceJobRequest(RequestModel):
        namespace: str = Field(min_length=1)
        dry_run: bool = Field(default=True, strict=True)

    class DecayRequest(NamespaceJobRequest):
        now: str | None = None

    class ReindexRequest(RequestModel):
        namespace: str | None = Field(default=None, min_length=1)
        indexes: list[str] = Field(default_factory=lambda: ["lexical", "vector"])
        dry_run: bool = Field(default=False, strict=True)

    class ForgetScopeRequest(RequestModel):
        event_id: str | None = Field(default=None, min_length=1)
        topic: str | None = Field(default=None, min_length=1)
        before: str | None = None
        after: str | None = None
        hard: bool = Field(default=False, strict=True)

    app = FastAPI(title="D.I.V.E. Memory API", version="0.2.0")
    service = MemoryService(db_path, extractor=extractor, embedder=embedder)
    app.state.memory_service = service

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Any, exc: RequestValidationError) -> JSONResponse:
        # Keep the public API's established 400 validation contract while
        # using strict Pydantic schemas internally.
        return JSONResponse(status_code=400, content={"detail": jsonable_encoder(exc.errors())})

    def check_scope(namespace: str, authorization: str | None, action: str) -> None:
        if authorizer is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "bearer token required")
        token = authorization.removeprefix("Bearer ").strip()
        if not token or not authorizer.authorize(token, namespace, action):
            raise HTTPException(403, "namespace access denied")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "storage": "sqlite"}

    @app.post("/v1/events")
    def ingest(body: EventRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "events:write")
        # ADR-005: ordinary events default to background projection, while an
        # explicit "remember" request gets immediate confirmation.
        defer = body.defer if body.defer is not None else not body.explicit
        try:
            return service.ingest(body.namespace, body.text, event_type=body.event_type,
                                  explicit=body.explicit, observed_at=body.observed_at,
                                  occurred_from=body.occurred_from, occurred_to=body.occurred_to,
                                  source_message_id=body.source_message_id,
                                  idempotency_key=body.idempotency_key, defer=defer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/jobs/process-outbox")
    def process_outbox(body: ProcessOutboxRequest | None = None,
                       authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope("*", authorization, "jobs:process")
        results = service.process_pending(body.limit if body else 100)
        return {"processed": len(results), "results": results}

    @app.post("/v1/memories/candidates")
    def submit_candidate(body: CandidateRequest,
                         authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "candidates:write")
        try:
            return service.ingest(body.namespace, body.text, event_type="memory_candidate",
                                  observed_at=body.observed_at, idempotency_key=body.idempotency_key,
                                  defer=body.defer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/retrieve")
    def retrieve(body: RetrieveRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "memories:read")
        try:
            result = service.retrieve(body.namespace, body.query, limit=body.limit,
                                      as_of=body.as_of, observed_as_of=body.observed_as_of,
                                      intent=body.intent, token_budget=body.token_budget)
        except ValueError as exc:
            # Unparsable as_of would otherwise silently answer a different
            # question than the caller asked.
            raise HTTPException(400, str(exc)) from exc
        packed = pack_context(result.items, body.token_budget)
        warnings = ["token budget omitted one or more candidates"] if packed.omitted else []
        abstain_reason = result.abstain_reason
        if result.items and not packed.items:
            abstain_reason = "token budget exhausted"
        return {"items": [{"memory": memory_payload(item.memory), "score": item.score,
                           "channels": item.channels, "source_refs": item.source_refs} for item in packed.items],
                "plan": result.plan, "query_plan": result.plan, "budget_used": packed.estimated_tokens,
                "warnings": warnings, "abstain_reason": abstain_reason, "degraded": result.degraded}

    @app.post("/v1/search")
    def search(body: RetrieveRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return retrieve(body, authorization)

    @app.post("/v1/context")
    def context(body: ContextRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "memories:read")
        try:
            result = service.retrieve_context(body.namespace, body.query, limit=body.limit,
                                              token_budget=body.token_budget, as_of=body.as_of,
                                              observed_as_of=body.observed_as_of)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"context": result["context"], "estimated_tokens": result["estimated_tokens"],
                "omitted": result["omitted"], "plan": result["plan"], "abstain_reason": result["abstain_reason"]}

    @app.get("/v1/memories/{memory_id}")
    def get_memory(memory_id: str, include_versions: bool = Query(default=False),
                   authorization: str | None = Header(default=None)) -> dict[str, Any]:
        memory = service.get_memory(memory_id)
        if memory is None:
            raise HTTPException(404, "memory not found")
        check_scope(memory.namespace, authorization, "memories:read")
        payload = memory_payload(memory)
        if include_versions:
            payload["versions"] = service.memory_versions(memory_id)
        return payload

    @app.patch("/v1/memories/{memory_id}")
    def correct_memory(memory_id: str, body: CorrectionRequest,
                       authorization: str | None = Header(default=None)) -> dict[str, Any]:
        existing = service.get_memory(memory_id)
        if existing is not None:
            check_scope(existing.namespace, authorization, "memories:write")
        try:
            return service.correct_memory(memory_id, body.content, namespace=body.namespace,
                                          idempotency_key=body.idempotency_key)
        except KeyError:
            raise HTTPException(404, "memory not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/memories/{memory_id}/reinforce")
    def reinforce(memory_id: str, body: ReinforceRequest | None = None,
                  authorization: str | None = Header(default=None)) -> dict[str, Any]:
        existing = service.get_memory(memory_id)
        if existing is not None:
            check_scope(existing.namespace, authorization, "memories:write")
        try:
            memory = service.reinforce(memory_id, namespace=body.namespace if body else None)
        except KeyError:
            raise HTTPException(404, "memory not found") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return memory_payload(memory)

    @app.get("/v1/users/{namespace}/memories")
    def list_memories(namespace: str, status: str | None = None, kind: str | None = None,
                      as_of: str | None = None, limit: int = Query(default=100, ge=1, le=1000),
                      offset: int = Query(default=0, ge=0),
                      authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(namespace, authorization, "memories:read")
        try:
            items = service.list_memories(namespace, status=status, kind=kind, as_of=as_of,
                                          limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"items": [memory_payload(memory) for memory in items]}

    @app.get("/v1/users/{namespace}/profile")
    def profile(namespace: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(namespace, authorization, "memories:read")
        return service.profile(namespace)

    @app.delete("/v1/memories/{memory_id}")
    def forget(memory_id: str, hard: bool = Query(default=False),
               authorization: str | None = Header(default=None)) -> dict[str, Any]:
        memory = service.get_memory(memory_id)
        if memory is None:
            raise HTTPException(404, "memory not found")
        check_scope(memory.namespace, authorization, "memories:delete")
        service.forget(memory_id, hard=hard)
        return {"deleted": True, "memory_id": memory_id, "hard": hard}

    @app.post("/v1/events/{event_id}/forget")
    def forget_event(event_id: str, hard: bool = Query(default=False),
                     authorization: str | None = Header(default=None)) -> dict[str, Any]:
        event = service.get_event(event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        check_scope(event.namespace, authorization, "memories:delete")
        return {"deleted_memory_ids": service.forget_event(event_id, hard=hard), "hard": hard}

    @app.post("/v1/users/{namespace}/forget")
    def forget_scope(namespace: str, body: ForgetScopeRequest,
                     authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(namespace, authorization, "memories:delete")
        try:
            return service.forget_scope(namespace, event_id=body.event_id, topic=body.topic,
                                        before=body.before, after=body.after, hard=body.hard)
        except KeyError:
            raise HTTPException(404, "event not found in namespace") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/memory-mode")
    def memory_mode(body: MemoryModeRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "memory-mode:write")
        service.set_memory_enabled(body.namespace, body.enabled)
        return {"namespace": body.namespace, "enabled": body.enabled}

    @app.get("/v1/users/{namespace}/timeline")
    def timeline(namespace: str, as_of: str | None = None, from_time: str | None = None,
                 to_time: str | None = None,
                 authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(namespace, authorization, "memories:read")
        try:
            items = service.timeline(namespace, as_of=as_of, from_time=from_time, to_time=to_time)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"items": [memory_payload(memory) for memory in items]}

    @app.get("/v1/export")
    def export_namespace(namespace: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(namespace, authorization, "memories:export")
        return service.export_namespace(namespace)

    @app.post("/v1/jobs/consolidate")
    def run_consolidation(body: NamespaceJobRequest,
                          authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "jobs:write")
        report = service.consolidate(body.namespace, dry_run=body.dry_run)
        return {"namespace": report.namespace, "examined": report.examined, "merged": report.merged,
                "archived": report.archived, "dry_run": report.dry_run}

    @app.post("/v1/jobs/decay")
    def decay(body: DecayRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace, authorization, "jobs:write")
        try:
            return service.decay(body.namespace, now=body.now, dry_run=body.dry_run)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/jobs/reindex")
    def reindex(body: ReindexRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(body.namespace or "*", authorization, "jobs:write")
        try:
            return service.reindex(body.namespace, indexes=body.indexes, dry_run=body.dry_run)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/jobs")
    def jobs(namespace: str | None = None, limit: int = Query(default=100, ge=1, le=1000),
             authorization: str | None = Header(default=None)) -> dict[str, Any]:
        check_scope(namespace or "*", authorization, "jobs:read")
        return {"items": service.jobs(namespace, limit=limit)}

    return app
