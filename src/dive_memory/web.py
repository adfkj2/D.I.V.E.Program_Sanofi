"""Local single-user workbench API over the shared application layer."""

import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .application.assistant import Assistant
from .application.casebook import load_casebook
from .application.demo import load_demo
from .application.project_import import analyze_folder
from .application.records import ProjectRecord
from .application.runtime import MemoryApplication
from .application.settings import application_from_environment
from .observability import RequestMetrics


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
    source_title: str = Field(default="", max_length=300)
    source_path: str = Field(default="", max_length=1000)
    source_excerpt: str = Field(default="", max_length=12000)
    source_sha256: str = Field(default="", max_length=64)


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


class ConsultInput(Input):
    question: str = Field(min_length=1, max_length=4000)
    as_of: str | None = None
    since: str | None = None


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(default="", max_length=1000)
    request_id: str = Field(min_length=1, max_length=200)


class ImportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=12000)


class FolderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    paths: list[str] = Field(max_length=2000)
    documents: list[ImportDocument] = Field(max_length=24)
    skipped: int = Field(default=0, ge=0, strict=True)


class ProjectCreateInput(ProjectInput):
    folder: FolderInput | None = None
    progress_note: str | None = Field(default=None, min_length=1, max_length=4000)
    next_step: str | None = Field(default=None, max_length=500)
    tasks: list[str] | None = Field(default=None, max_length=20)


class TaskInput(Input):
    title: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=200)


class TaskUpdate(Input):
    title: str = Field(min_length=1, max_length=500)
    completed: bool = Field(strict=True)
    revision: int = Field(ge=1, strict=True)


class CheckpointInput(Input):
    note: str = Field(min_length=1, max_length=4000)
    next_step: str = Field(default="", max_length=500)
    request_id: str = Field(min_length=1, max_length=200)


class ChatInput(Input):
    message: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=200)


class DecisionInput(Input):
    approve: bool = Field(strict=True)


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    endpoint: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=1000, repr=False)


def create_workbench(
    application: MemoryApplication | None = None,
    *,
    access_token: str | None = None,
    assistant_model=None,
) -> FastAPI:
    runtime = application or application_from_environment()
    token = (
        access_token
        if access_token is not None
        else os.getenv("DIVE_WORKBENCH_TOKEN", "")
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(
        title="DIVE Project Memory",
        version="0.4.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.application = runtime
    assistant = Assistant(runtime, model=assistant_model)
    app.state.assistant = assistant
    telemetry = RequestMetrics()
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        started = time.perf_counter()
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            response = JSONResponse(
                {"detail": "cross-origin request denied"}, status_code=403
            )
        elif (
            request.url.path.startswith("/api/")
            and token
            and not secrets.compare_digest(
                request.headers.get("authorization", "").removeprefix("Bearer "), token
            )
        ):
            response = JSONResponse(
                {"detail": "access token required"}, status_code=401
            )
        else:
            response = await call_next(request)
        # Observability: labels use the matched route template (static set), so
        # cardinality stays bounded even under arbitrary-path probing; requests
        # that never matched a route collapse into a single "unmatched" label.
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        telemetry.record(path, response.status_code, time.perf_counter() - started)
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

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == "/api/assistant/config":
            # Pydantic's default errors contain the rejected input, including keys.
            return JSONResponse(
                {"detail": "请检查模型地址、名称和密钥长度。"}, status_code=422
            )
        return await request_validation_exception_handler(request, exc)

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
        if name not in {"app.js", "assistant.js", "app.css", "icon.svg"}:
            raise HTTPException(404)
        return Response(
            files("dive_memory").joinpath("static", name).read_text(encoding="utf-8"),
            media_type={
                "app.js": "application/javascript",
                "assistant.js": "application/javascript",
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

    @app.get("/metrics")
    def metrics():
        """Prometheus text exposition; stdlib-only so the wheel stays dependency-free."""
        try:
            readiness = runtime.readiness()
        except sqlite3.Error:  # readiness probe must never take the scrape down
            readiness = {"ready": False, "database": "unavailable"}
        return Response(
            telemetry.render(
                ready=bool(readiness["ready"]),
                database_ready=readiness.get("database") == "ready",
                mode=runtime.mode,
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/snapshot")
    def snapshot(project: str, as_of: str | None = None):
        return runtime.snapshot(project, as_of=as_of)

    @app.get("/api/projects")
    def projects():
        return {"projects": runtime.projects()}

    @app.get("/api/assistant/config")
    def assistant_config():
        return assistant.config()

    @app.post("/api/assistant/config")
    def configure_assistant(body: ModelInput):
        return assistant.configure(body.endpoint, body.model, body.api_key)

    @app.get("/api/assistant/history")
    def assistant_history(project: str):
        return {"turns": assistant.history(project)}

    @app.post("/api/assistant/chat")
    def assistant_chat(body: ChatInput):
        return assistant.chat(body.project, body.message, body.request_id)

    @app.post("/api/assistant/runs/{run_id}/decision")
    def assistant_decision(run_id: str, body: DecisionInput):
        return assistant.decide(body.project, run_id, body.approve)

    @app.get("/api/project")
    def project_details(project: str):
        return runtime.project_details(project)

    @app.post("/api/project-import/preview")
    def preview_project_import(body: FolderInput):
        result = analyze_folder(body.model_dump())
        # The browser already holds the documents; avoid echoing full contents.
        result["sources"] = [
            {key: source[key] for key in ("path", "sha256")}
            for source in result["sources"]
        ]
        return result

    @app.post("/api/projects")
    def create_project(body: ProjectCreateInput):
        return runtime.create_project(
            body.name,
            body.goal,
            body.request_id,
            folder=body.folder.model_dump() if body.folder is not None else None,
            progress_note=body.progress_note,
            next_step=body.next_step,
            tasks=body.tasks,
        )

    @app.put("/api/project")
    def update_project(body: ProjectInput, project: str):
        return runtime.update_project(project, body.name, body.goal)

    @app.post("/api/tasks")
    def add_task(body: TaskInput):
        return runtime.add_task(body.project, body.title, body.request_id)

    @app.put("/api/tasks/{identifier}")
    def update_task(identifier: str, body: TaskUpdate):
        return runtime.update_task(
            body.project, identifier, body.title, body.completed, body.revision
        )

    @app.post("/api/checkpoints")
    def checkpoint(body: CheckpointInput):
        return runtime.checkpoint(
            body.project, body.note, body.next_step, body.request_id
        )

    @app.get("/api/backup")
    def backup():
        handle = tempfile.NamedTemporaryFile(
            prefix="dive-backup-", suffix=".db", delete=False
        )
        path = Path(handle.name)
        handle.close()
        try:
            runtime.backup(str(path))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename="续研-完整备份.db",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.get("/api/lookup")
    def lookup(project: str, key: str, as_of: str | None = None):
        return runtime.lookup(project, key, as_of=as_of)

    @app.get("/api/brief")
    def brief(project: str, as_of: str | None = None, since: str | None = None):
        return runtime.brief(project, as_of=as_of, since=since)

    @app.post("/api/consult")
    def consult(body: ConsultInput):
        return runtime.consult(
            body.project, body.question, as_of=body.as_of, since=body.since
        )

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
                        "source_title",
                        "source_path",
                        "source_excerpt",
                        "source_sha256",
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

    @app.post("/api/casebook")
    def casebook(body: Input):
        return load_casebook(runtime, body.project)

    @app.get("/api/verification")
    def verification():
        # Packaged evidence is data with an explicit run/provenance label; never
        # inferred from the current service's memory count or hard-coded in JS.
        import json

        path = files("dive_memory").joinpath("static/verification.json")
        return json.loads(path.read_text(encoding="utf-8"))

    return app
