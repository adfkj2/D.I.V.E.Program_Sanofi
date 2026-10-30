"""Local-only HTTP composition root. Run wot or python -m wot.main, one worker only."""
import ipaddress
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .catalog import skill_summary
from .domain import DomainError
from .model import Provider
from .service import WotService
from .upgrade import Upgrade


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Conversation(Input):
    title: str = Field(default="一段新的对话", min_length=1, max_length=120)
    skill_id: str | None = None


class Message(Input):
    text: str = Field(min_length=1, max_length=12_000)
    remember: bool = False
    domain: str = Field(default="general", min_length=1, max_length=80)


class Goal(Input):
    objective: str = Field(min_length=1, max_length=2000)
    domain: str = Field(default="general", min_length=1, max_length=80)
    limits: str = Field(default="", max_length=2000)


class Revision(Input):
    revision: int = Field(ge=1)


class Correction(Input):
    text: str = Field(min_length=1, max_length=12_000)
    domain: str = Field(default="general", min_length=1, max_length=80)


class Skill(Input):
    text: str = Field(min_length=1, max_length=64_000)
    category: str
    permission: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=2000)


class Simulation(Input):
    topic: str = Field(min_length=1, max_length=2000)
    persona_version_id: str
    skill_id: str
    max_turns: int = Field(default=6, ge=1, le=20)
    max_tokens: int = Field(default=6000, ge=400, le=30_000)
    allow_private_context: bool = False


class Feedback(Input):
    verdict: str = Field(pattern="^(LIKE_ME|NOT_LIKE_ME|UNCERTAIN|WANT_TO_BE)$")
    comment: str = Field(default="", max_length=2000)
    intent_scope: str = "SIM_STYLE_CORRECTION"


class Growth(Input):
    directive_id: str
    title: str = Field(min_length=1, max_length=200)
    trigger: str = Field(min_length=1, max_length=2000)
    action: str = Field(min_length=1, max_length=2000)
    user_approved: bool = False


class Checkin(Input):
    outcome: str = Field(min_length=1, max_length=4000)


class Empty(Input):
    pass


class NextTurn(Input):
    disclosure_approved: bool = False


def create_app(data_dir: str | Path | None = None, provider: Provider | None = None) -> FastAPI:
    directory = Path(data_dir or os.getenv("WOT_DATA_DIR", str(Path.cwd() / ".local")))
    service = WotService(directory, provider)
    upgrade = Upgrade(service, directory, provider)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        upgrade.virtual.start_scheduler()
        yield
        upgrade.close()
        service.close()

    app = FastAPI(title="我们曾聊过", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.upgrade = upgrade
    app.include_router(upgrade.router())
    app.include_router(upgrade.media_router())
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    origins = {f"http://{host}:{port}" for host in ("localhost", "127.0.0.1")
               for port in (8765, 5173)}

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        host = request.url.hostname
        origin = request.headers.get("origin")
        try:
            local_peer = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local_peer = request.client is not None and request.client.host == "testclient"
        if not local_peer:
            return JSONResponse({"detail": "仅允许本机连接"}, 403)
        if host not in allowed_hosts:
            return JSONResponse({"detail": "仅允许本机访问"}, 403)
        if origin and origin not in origins:
            return JSONResponse({"detail": "请求来自未授权页面"}, 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "拒绝跨站请求"}, 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-wot-token", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "本地会话已改变，请刷新页面重试"}, 403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/api/v1/healthz")
    def health():
        return {"status": "ok", "mode": "LOCAL_RESEARCH_PREVIEW",
                "test_server": bool(provider is not None and getattr(provider, "test_server", False))}

    @app.get("/api/v1/session")
    def session():
        return {"token": token}

    @app.get("/api/v1/state")
    def state():
        return {**service.state(), "upgrade": upgrade.state()}

    # All mutations get a stable request identity. The UI supplies its own;
    # omitted identities are one-shot, while resource operations also guard duplicates.
    def mutation(request: Request, data: dict, action):
        key = request.headers.get("idempotency-key") or secrets.token_urlsafe(24)
        return service.once(key, {"path": request.url.path, **data}, action)

    @app.post("/api/v1/real-conversations")
    def conversation(body: Conversation, request: Request):
        return mutation(request, body.model_dump(), lambda: service.conversation(body.title, body.skill_id))

    @app.get("/api/v1/real-conversations/{rid}/messages")
    def messages(rid: str):
        return service.messages(rid)

    @app.post("/api/v1/real-conversations/{rid}/messages")
    def send(rid: str, body: Message, request: Request):
        key = request.headers.get("idempotency-key")
        if not key:
            raise DomainError("发送消息需要 Idempotency-Key")
        return mutation(request, body.model_dump(), lambda: service.send(
            rid, body.text, body.remember, body.domain, key))

    @app.post("/api/v1/real-messages/{rid}/admit")
    def admit(rid: str, body: Empty, request: Request):
        return mutation(request, {}, lambda: service.admit(rid))

    @app.get("/api/v1/evidence")
    def evidence(domain: str | None = None):
        return [e for e in service.active_evidence() if domain is None or e["domain"] == domain]

    @app.post("/api/v1/evidence/{rid}/corrections")
    def correct(rid: str, body: Correction, request: Request):
        return mutation(request, body.model_dump(), lambda: service.correct(rid, body.text, body.domain))

    @app.delete("/api/v1/data/evidence/{rid}")
    def delete(rid: str, request: Request):
        return mutation(request, {}, lambda: service.delete(rid))

    @app.get("/api/v1/personas/mirror")
    def mirror():
        return service.mirror()

    @app.post("/api/v1/personas/chosen/proposals")
    def propose(body: Goal, request: Request):
        return mutation(request, body.model_dump(), lambda: service.propose(**body.model_dump()))

    @app.post("/api/v1/personas/chosen/proposals/{rid}/approve")
    def approve(rid: str, body: Revision, request: Request):
        return mutation(request, body.model_dump(), lambda: service.directive(rid, body.revision, "approve"))

    @app.post("/api/v1/personas/chosen/proposals/{rid}/revoke")
    def revoke(rid: str, body: Revision, request: Request):
        return mutation(request, body.model_dump(), lambda: service.directive(rid, body.revision, "revoke"))

    @app.post("/api/v1/personas/{track}/compile")
    def compile(track: str, body: Empty, request: Request):
        return mutation(request, {}, lambda: service.compile(track))

    @app.get("/api/v1/personas/versions")
    def versions():
        return service.store.all("version")

    @app.post("/api/v1/skills/import")
    def import_skill(body: Skill, request: Request):
        # Legacy cached import receipts must not reactivate unreviewed roles.
        return upgrade.people.draft(body.model_dump())

    @app.get("/api/v1/skills/{rid}")
    def skill_detail(rid: str):
        return skill_summary(service.require(rid, "skill"))

    @app.post("/api/v1/simulations")
    def simulation(body: Simulation, request: Request):
        upgrade.assessments.assert_unlocked()
        raise DomainError("旧预览记录仅供回看，请在虚拟空间新建会话", 409)

    @app.post("/api/v1/simulations/{rid}/next")
    def next_turn(rid: str, body: NextTurn, request: Request):
        upgrade.assessments.assert_unlocked()
        raise DomainError("旧预览记录仅供回看，不能继续生成", 409)

    @app.post("/api/v1/simulations/{rid}/{action}")
    async def control(rid: str, action: str, body: Empty):
        if action not in {"pause", "resume", "cancel"}:
            raise DomainError("找不到操作", 404)
        # No generation lock: cancel/pause can interrupt an in-flight request.
        return await run_in_threadpool(service.control, rid, action)

    @app.post("/api/v1/simulation-turns/{rid}/feedback")
    def feedback(rid: str, body: Feedback, request: Request):
        return mutation(request, body.model_dump(), lambda: service.feedback(rid, **body.model_dump()))

    @app.post("/api/v1/growth/goals")
    def growth(body: Growth, request: Request):
        return mutation(request, body.model_dump(), lambda: service.growth(**body.model_dump()))

    @app.post("/api/v1/growth/actions/{rid}/checkins")
    def checkin(rid: str, body: Checkin, request: Request):
        return mutation(request, body.model_dump(), lambda: service.checkin(rid, body.outcome))

    @app.post("/api/v1/growth/actions/{rid}/revoke")
    def revoke_action(rid: str, body: Empty, request: Request):
        return mutation(request, {}, lambda: service.checkin(rid, None))

    @app.post("/api/v1/data/export")
    def export(body: Empty):
        return {**service.export(), "profile": upgrade.people.me(),
                "virtual_sessions": upgrade.virtual.list()}

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    portraits = frontend / "portraits"
    if not portraits.is_dir():
        portraits = frontend.parent / "public" / "portraits"
    if portraits.is_dir():
        app.mount("/portraits", StaticFiles(directory=portraits), name="portraits")
    if frontend.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/{path:path}")
        def index(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "找不到接口"}, 404)
            return FileResponse(frontend / "index.html")

    return app


def main():
    # Explicit vendor path for source checkout; pip-installable core is also supported.
    uvicorn.run("wot.main:create_app", factory=True, host="127.0.0.1", port=8765, workers=1)


if __name__ == "__main__":
    sys.exit(main())
