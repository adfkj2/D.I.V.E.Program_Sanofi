"""Local single-user workbench API over the shared application layer."""

import os
import secrets
from contextlib import asynccontextmanager
from importlib.resources import files
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .application.demo import load_demo
from .application.records import ProjectRecord
from .application.runtime import MemoryApplication
from .application.settings import application_from_environment


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project: str = Field(min_length=1, max_length=120)


class RecordInput(Input):
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=6000)
    category: str = "note"
    disposition: str = "confirmed"
    reason: str = Field(default="", max_length=2000)
    session: str = Field(default="default", min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=200)
    observed_at: str | None = None
    replaces: str | None = None


class EventInput(Input):
    text: str = Field(min_length=1, max_length=12000)
    session: str = Field(default="default", min_length=1, max_length=120)
    explicit: bool = Field(default=False, strict=True)
    observed_at: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)


class AskInput(Input):
    question: str = Field(min_length=1, max_length=4000)
    as_of: str | None = None
    token_budget: int = Field(default=1500, ge=1, le=12000, strict=True)
    limit: int = Field(default=12, ge=1, le=100, strict=True)


def create_workbench(
    application: MemoryApplication | None = None, *, access_token: str | None = None
) -> FastAPI:
    runtime = application or application_from_environment()
    token = (
        access_token
        if access_token is not None
        else os.getenv("DIVE_WORKBENCH_TOKEN", "")
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        runtime.close()

    app = FastAPI(
        title="DIVE Project Memory",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.application = runtime
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {"detail": "cross-origin request denied"}, status_code=403
            )
        if request.url.path.startswith("/api/") and token:
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse(
                    {"detail": "access token required"}, status_code=401
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse(
            {"detail": "record not found in this project, or trace expired"},
            status_code=404,
        )

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (
            files("dive_memory")
            .joinpath("static/index.html")
            .read_text(encoding="utf-8")
        )

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "app.css", "icon.svg"}:
            raise HTTPException(404)
        return Response(
            files("dive_memory").joinpath("static", name).read_text(encoding="utf-8"),
            media_type={
                "app.js": "application/javascript",
                "app.css": "text/css",
                "icon.svg": "image/svg+xml",
            }[name],
        )

    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    @app.get("/api/readiness")
    def ready():
        result = runtime.readiness()
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @app.get("/api/snapshot")
    def snapshot(project: str, as_of: str | None = None):
        return runtime.snapshot(project, as_of=as_of)

    @app.get("/api/lookup")
    def lookup(project: str, key: str, as_of: str | None = None):
        return runtime.lookup(project, key, as_of=as_of)

    @app.post("/api/records")
    def record(body: RecordInput):
        values = body.model_dump()
        return runtime.record(
            values.pop("project"),
            ProjectRecord(
                **{
                    k: values[k]
                    for k in (
                        "key",
                        "value",
                        "category",
                        "disposition",
                        "reason",
                        "session",
                    )
                }
            ),
            idempotency_key=body.idempotency_key,
            observed_at=body.observed_at,
            replaces=body.replaces,
        )

    @app.post("/api/events")
    def event(body: EventInput):
        return runtime.ingest(
            body.project,
            body.text,
            idempotency_key=body.idempotency_key,
            observed_at=body.observed_at,
            session=body.session,
            explicit=body.explicit,
        )

    @app.post("/api/answer")
    def answer(body: AskInput):
        return runtime.answer(
            body.project,
            body.question,
            as_of=body.as_of,
            token_budget=body.token_budget,
            limit=body.limit,
        )

    @app.get("/api/sources/{memory_id}")
    def source(memory_id: str, project: str):
        return runtime.source(project, memory_id)

    @app.get("/api/events/{event_id}")
    def inspect_event(event_id: str, project: str):
        return runtime.inspect_event(project, event_id)

    @app.delete("/api/memories/{memory_id}")
    def forget(memory_id: str, project: str):
        return runtime.forget(project, memory_id)

    @app.get("/api/traces/{trace_id}")
    def trace(trace_id: str, project: str):
        return runtime.trace(project, trace_id)

    @app.post("/api/demo")
    def demo(body: Input):
        return load_demo(runtime, body.project)

    @app.get("/api/verification")
    def verification():
        # Packaged evidence is data with an explicit run/provenance label; never
        # inferred from the current service's memory count or hard-coded in JS.
        import json

        path = files("dive_memory").joinpath("static/verification.json")
        return json.loads(path.read_text(encoding="utf-8"))

    return app
