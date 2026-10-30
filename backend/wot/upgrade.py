"""Upgrade composition and local-owner routes. No secret-bearing generic state."""

import re

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from .assessments import AssessmentManager
from .connections import ConnectionManager
from .domain import DomainError
from .people import PeopleManager
from .virtual import VirtualSpace


class Upgrade:
    def __init__(self, service, directory, provider=None):
        self.service = service
        self.connections = ConnectionManager(service.store, directory)
        if provider is not None:
            self.connections._fallback = provider
            self.connections.calls = provider.calls
        self.people = PeopleManager(service, directory)
        self.assessments = AssessmentManager(
            service.store,
            directory,
            self.connections,
            self.people.user_skill,
            lambda rid: service.require(rid, "skill_import"),
        )
        self.people.assessments = self.assessments
        self.virtual = VirtualSpace(service, self.people, self.connections, self.assessments)
        service.provider = self.connections
        service.people = self.people
        service.assessments = self.assessments
        service.invalidate_hooks.extend([self.people.invalidate, self.virtual.invalidate])

    def state(self):
        return {
            "connections": self.connections.list_connections(),
            "providers": self.connections.providers(),
            "me": self.people.me(),
            "unlock": self.assessments.unlock_state(),
            "preferences": self.people.preferences(),
            "skill_imports": self.service.store.all("skill_import"),
            "virtual_sessions": self.virtual.list(),
        }

    def close(self):
        self.virtual.close()
        self.assessments.close()
        self.connections.close()

    def router(self):
        r = APIRouter(prefix="/api/v1")
        c, p, a, v = self.connections, self.people, self.assessments, self.virtual

        @r.get("/model-providers")
        def providers():
            return c.providers()

        @r.post("/model-discovery")
        def discover_models(data: dict = Body(...)):
            return c.discover(data)

        @r.get("/model-connections")
        def connections():
            return c.list_connections()

        @r.post("/model-connections")
        def save_connection(data: dict = Body(...)):
            return c.save(data)

        @r.post("/model-connections/{rid}")
        def edit_connection(rid: str, data: dict = Body(...)):
            return c.save(data, rid)

        @r.delete("/model-connections/{rid}")
        def remove_connection(rid: str):
            return c.remove(rid)

        @r.post("/model-connections/{rid}/test")
        def test_connection(rid: str):
            return c.test(rid)

        @r.post("/model-connections/{rid}/default")
        def default_connection(rid: str):
            return c.set_default(rid)

        @r.post("/model-connections/{rid}/models")
        def models(rid: str):
            return c.models(rid)

        @r.get("/me")
        def me():
            return p.me()

        @r.post("/me")
        def save_me(data: dict = Body(...)):
            p.profile(data)
            return p.me()

        @r.get("/me/versions")
        def versions():
            return p.me()["versions"]

        @r.post("/me/avatar")
        def my_avatar(data: dict = Body(...)):
            return p.avatar("my_profile", data.get("data_url"))

        @r.post("/me/avatar/reset")
        def reset_my_avatar():
            return p.avatar("my_profile", reset=True)

        @r.post("/preferences")
        def preferences(data: dict = Body(...)):
            return p.preferences(data)

        @r.get("/skill-imports")
        def imports():
            return self.service.store.all("skill_import")

        @r.post("/skill-imports")
        def create_import(data: dict = Body(...)):
            return p.draft(data)

        @r.post("/skill-imports/{rid}")
        def edit_import(rid: str, data: dict = Body(...)):
            return p.draft(data, rid)

        @r.post("/skill-imports/{rid}/assess")
        def assess_import(rid: str):
            task = a.start("QUALITY", rid)
            with self.service.store.transaction():
                draft = self.service.require(rid, "skill_import")
                draft["assessment_id"] = task["id"]
                self.service.store.update(draft)
            return task

        @r.post("/skill-imports/{rid}/add")
        def add_import(rid: str):
            return p.add(rid)

        @r.delete("/skills/{rid}")
        def remove_skill(rid: str):
            result = p.delete(rid)
            v.invalidate(rid, "参与人物已移出可用列表")
            return result

        @r.post("/skills/{rid}/restore")
        def restore_skill(rid: str):
            return p.restore(rid)

        @r.post("/skills/{rid}/avatar")
        def avatar(rid: str, data: dict = Body(...)):
            return p.avatar(rid, data.get("data_url"))

        @r.post("/skills/{rid}/avatar/reset")
        def reset_avatar(rid: str):
            return p.avatar(rid, reset=True)

        @r.get("/builtin-catalog")
        def catalog():
            return p.catalog()

        @r.get("/assessments")
        def assessments():
            return a.list()

        @r.post("/assessments")
        def start_assessment(data: dict = Body(...)):
            if set(data) - {"kind", "subject_id"}:
                raise DomainError("评估参数包含未知字段")
            return a.start(data.get("kind", "PERSONAL"), data.get("subject_id"))

        @r.get("/assessments/{rid}")
        def assessment(rid: str):
            return a.get(rid)

        @r.post("/assessments/{rid}/answers")
        def answers(rid: str, data: dict = Body(...)):
            if set(data) != {"answers"}:
                raise DomainError("请仅提交本人答案")
            return a.answer(rid, data["answers"])

        @r.post("/assessments/{rid}/reviews")
        def reviews(rid: str, data: dict = Body(...)):
            if set(data) != {"reviews"}:
                raise DomainError("请仅提交本人核验")
            return a.review(rid, data["reviews"])

        @r.post("/assessments/{rid}/confirm")
        def confirm(rid: str):
            return a.confirm(rid)

        @r.post("/assessments/{rid}/cancel")
        def cancel(rid: str):
            return a.cancel(rid)

        @r.post("/assessments/{rid}/resume")
        def resume(rid: str):
            return a.resume(rid)

        @r.get("/virtual-participants")
        def participants():
            return v.participants()

        @r.get("/virtual-sessions")
        def virtual_sessions():
            return v.list()

        @r.post("/virtual-sessions")
        def create_virtual(data: dict = Body(...)):
            return v.create(data)

        @r.post("/virtual-sessions/pause-all")
        def pause_all():
            return v.pause_all()

        @r.get("/virtual-sessions/{rid}")
        def virtual_session(rid: str):
            return v.view(rid)

        @r.get("/virtual-sessions/{rid}/events")
        def events(rid: str):
            session = v.view(rid)
            return {"revision": session["revision"], "session": session}

        @r.post("/virtual-sessions/{rid}/takeover")
        def takeover(rid: str):
            return v.takeover(rid)

        @r.post("/virtual-sessions/{rid}/interventions")
        def intervention(rid: str, data: dict = Body(...)):
            return v.intervene(rid, data)

        @r.post("/virtual-sessions/{rid}/{action}")
        def control(rid: str, action: str):
            return v.control(rid, action)

        return r

    def media_router(self):
        router = APIRouter()

        @router.get("/user-avatars/{filename}")
        def avatar_file(filename: str):
            if not re.fullmatch(r"[0-9a-f]{64}\.png", filename):
                raise DomainError("找不到头像", 404)
            path = self.people.media / filename
            if not path.is_file():
                raise DomainError("找不到头像", 404)
            return FileResponse(path, media_type="image/png")

        return router
