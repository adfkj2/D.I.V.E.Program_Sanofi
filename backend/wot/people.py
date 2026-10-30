"""Human-owned profiles, versioned role drafts and re-encoded local avatars."""

import base64
import binascii
import io
import json
import re
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .catalog import builtin_people, skill_summary
from .domain import DomainError, SourceOrigin, classify
from .storage import digest, now, uid


class PeopleManager:
    def __init__(self, service, directory: Path):
        self.service, self.store = service, service.store
        self.media = directory / "avatars"
        self.media.mkdir(parents=True, exist_ok=True)
        self.assessments = None
        with self.store.transaction():
            if not self.store.get("my_profile", "profile"):
                self.store.add(
                    "profile", {"nickname": "我", "bio": "", "avatar_url": ""}, rid="my_profile"
                )
            if not self.store.get("preferences", "preferences"):
                self.store.add("preferences", {"setup_dismissed": False}, rid="preferences")
            # Imported roles from older releases retain IDs and histories, but
            # cannot acquire admission through a migration.
            for role in self.store.all("skill"):
                if (
                    not role.get("builtin")
                    and role["id"] != "skill_socratic"
                    and not role.get("quality_assessment_id")
                ):
                    if role.get("status") == "ACTIVE":
                        role["status"] = "PENDING_REVIEW"
                        self.store.update(role)
                    if not self.store.get("draft_" + role["id"], "skill_import"):
                        self.store.add(
                            "skill_import",
                            {
                                **role,
                                "id": "draft_" + role["id"],
                                "existing_skill_id": role["id"],
                                "version": 1,
                                "status": "DRAFT",
                                "content_hash": digest(role.get("text", "")),
                            },
                            rid="draft_" + role["id"],
                        )

    def preferences(self, data=None):
        value = self.store.get("preferences", "preferences")
        if data is not None:
            if set(data) - {"setup_dismissed"} or not isinstance(data.get("setup_dismissed"), bool):
                raise DomainError("设置内容无效")
            with self.store.transaction():
                value.update(data)
                self.store.update(value)
        return value

    def profile(self, data=None):
        value = self.store.get("my_profile", "profile")
        if data is not None:
            if set(data) - {"nickname", "bio", "self_statement", "statement_confirmed"}:
                raise DomainError("个人资料包含未知字段")
            for field, limit in (("nickname", 80), ("bio", 4000)):
                if field in data and (not isinstance(data[field], str) or len(data[field]) > limit):
                    raise DomainError("请缩短个人资料")
            if data.get("self_statement"):
                if data.get("statement_confirmed") is not True:
                    raise DomainError("请确认这是你本人的真实陈述")
                self.statement(data["self_statement"], "USER_SELF_REPORT")
            with self.store.transaction():
                value.update({k: data[k] for k in ("nickname", "bio") if k in data})
                self.store.update(value)
        return value

    def statement(self, text, origin="USER_CORRECTION", domain="general"):
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise DomainError("请填写有效的本人陈述")
        if origin not in {"USER_SELF_REPORT", "USER_CORRECTION"}:
            raise DomainError("此来源不能成为本人证据", 403)
        if classify(text) == "ASPIRATION":
            raise DomainError("理想方向请在目标中确认，不能作为当前真实画像")
        with self.service.lock, self.store.transaction():
            eid = uid("ev")
            self.store.db.execute(
                "INSERT INTO evidence(id,owner_id,subject_id,message_id,origin,claim_kind,domain,text,"
                "namespace,stable_key,status,created_at,occurred_at,consent_scope,visibility_scope,content_hash) "
                "VALUES(?, 'local','local',NULL,?,?,?,?,?,?,'PENDING',?,?,'MIRROR','PRIVATE',?)",
                (
                    eid,
                    origin,
                    "SELF_REPORT",
                    domain,
                    text,
                    "user/local/self_reports",
                    digest([eid, text]),
                    now(),
                    now(),
                    digest(text),
                ),
            )
            self.store.db.execute(
                "INSERT INTO outbox(id,evidence_id,operation,state) VALUES(?,?,'INGEST','PENDING')",
                (uid("out"), eid),
            )
            self.store.audit("HUMAN_CONFIRMED_STATEMENT", eid)
        self.service.recover(eid)
        return self.service.evidence(eid)

    def user_skill(self, version_id=None):
        # Snapshot extraction and publication share deletion's serialization
        # boundary; no ACTIVE version can be compiled from a withdrawn source.
        with self.store.transaction():
            return self._user_skill(version_id)

    def _user_skill(self, version_id=None):
        if version_id:
            version = self.store.get(version_id, "user_skill")
            if not version:
                raise DomainError("找不到本人画像版本", 404)
            return version
        profile = self.profile()
        evidence = [
            e
            for e in self.service.active_evidence()
            if e["claim_kind"] in {"SELF_REPORT", "OBSERVATION"}
        ]
        manifest = [
            {"id": e["id"], "hash": e["content_hash"], "origin": e["origin"]} for e in evidence
        ]
        goals = [d for d in self.store.all("directive") if d["state"] == "APPROVED"]
        fingerprint = digest(
            {"evidence": manifest, "bio": profile["bio"],
             "chosen_aspirations": goals, "compiler": "evidence-skill-v1"}
        )
        rid = "user_skill_" + fingerprint
        existing = self.store.get(rid, "user_skill")
        if existing:
            return existing
        mirror = self.service.mirror()
        text = (
            "仅根据已授权本人真实资料模拟；资料不足时明确不确定，不编造经历。"
            "自我陈述与行为推断分别标记，理想方向不作为现有事实。\n"
            + json.dumps(
                {
                    "self_declared_bio": profile["bio"],
                    "PERCEIVED": mirror["PERCEIVED"],
                    "OBSERVED": mirror["OBSERVED"],
                    "CHOSEN_ASPIRATIONS_NOT_FACTS": goals,
                },
                ensure_ascii=False,
            )
        )
        with self.store.transaction():
            version = self.store.add(
                "user_skill",
                {
                    "text": text,
                    "content_hash": fingerprint,
                    "evidence_ids": [e["id"] for e in evidence],
                    "directive_ids": [d["id"] for d in goals],
                    "status": "ACTIVE",
                    "cutoff": now(),
                    "bio": profile["bio"],
                    "summary": "\n".join(e["text"] for e in evidence),
                    "compiler": "evidence-skill-v1",
                    "source_manifest": manifest,
                },
                rid=rid,
            )
            for e in evidence:
                self.store.link(e["id"], rid)
            for d in goals:
                self.store.link(d["id"], rid)
        return version

    def me(self):
        draft = self.user_skill()
        unlock = self.assessments.unlock_state() if self.assessments else {"unlocked": False}
        grant = unlock.get("grant") or {}
        certified_id = (
            grant.get("profile_version_id") or grant.get("user_skill_id") or grant.get("version_id")
        )
        certified = self.store.get(certified_id, "user_skill") if certified_id else None
        return {
            **self.profile(),
            "self_statements": [
                e
                for e in self.service.active_evidence()
                if e["origin"]
                in {SourceOrigin.USER_SELF_REPORT.value, SourceOrigin.USER_CORRECTION.value}
            ],
            "mirror": self.service.mirror(),
            "goals": self.store.all("directive"),
            "versions": self.store.all("user_skill"),
            "draft": draft,
            "certified": certified,
            "assessments": self.assessments.list() if self.assessments else [],
        }

    def draft(self, data, rid=None):
        allowed = {"text", "display_name", "description", "category", "permission", "source"}
        if set(data) - allowed:
            raise DomainError("人物资料包含未知字段")
        previous = self.service.require(rid, "skill_import") if rid else {}
        text = data.get("text", previous.get("text", ""))
        # JSON files are data documents too, never tool definitions.
        if isinstance(text, str) and text.lstrip().startswith("{"):
            try:
                document = json.loads(text)
                if isinstance(document, dict) and isinstance(document.get("text"), str):
                    data = {
                        **{k: v for k, v in document.items() if k in allowed},
                        **data,
                        "text": document["text"],
                    }
                    text = data["text"]
            except json.JSONDecodeError:
                pass
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 64000:
            raise DomainError("人物资料需为 1–64KB 的文本")
        for field, limit in (("display_name", 120), ("description", 2000),
                             ("source", 4000), ("permission", 4000), ("category", 30)):
            if field in data and (not isinstance(data[field], str) or len(data[field]) > limit):
                raise DomainError("人物名称、简介、来源和许可需为长度适中的文字")
        name = data.get("display_name") or previous.get("display_name")
        if not name:
            match = re.search(r"(?m)^name:\s*(.+)$", text)
            name = match[1].strip("\"'") if match else "新人物"
        value = {
            **previous,
            **data,
            "display_name": name[:120],
            "text": text,
            "description": data.get(
                "description", previous.get("description", "用户提供的解释性 AI 角色")
            ),
            "source": data.get("source", previous.get("source", "")),
            "permission": data.get("permission", previous.get("permission", "")),
            "category": data.get("category", previous.get("category", "PRIVATE")),
            "version": int(previous.get("version", 0)) + 1,
            "content_hash": digest(text),
            "status": "DRAFT",
        }
        if value["category"] not in {"PRIVATE", "CONSENTED", "CURATED"}:
            raise DomainError("请选择有效的人物类型")
        value["content_hash"] = digest(
            {
                k: value[k]
                for k in ("text", "source", "permission", "category", "description", "display_name")
            }
        )
        value.pop("assessment_id", None)
        with self.store.transaction():
            if previous:
                self.store.update(value)
            else:
                value = self.store.add("skill_import", value)
        return value

    def add(self, rid):
        with self.store.transaction():
            draft = self.service.require(rid, "skill_import")
            assessment = self.assessments.quality_ready(rid)
            role = {
                k: v for k, v in draft.items() if k not in {"id", "created_at", "existing_skill_id"}
            }
            role.update(status="ACTIVE", builtin=False, quality_assessment_id=assessment["id"])
            existing = draft.get("existing_skill_id") or draft.get("added_skill_id")
            if existing:
                old = self.service.require(existing, "skill")
                old.update(role)
                self.store.update(old)
                result = old
            else:
                result = self.store.add("skill", role)
            draft.update(status="ADDED", added_skill_id=result["id"])
            self.store.update(draft)
        return skill_summary(result)

    def delete(self, rid):
        role = self.service.require(rid, "skill")
        with self.store.transaction():
            role.update(status="DELETED", deleted_at=now())
            self.store.update(role)
            self.store.audit("CHARACTER_REMOVED", rid)
        return skill_summary(role)

    def restore(self, rid):
        role = self.service.require(rid, "skill")
        if not role.get("builtin"):
            raise DomainError("自定义人物需要重新评估后添加", 409)
        with self.store.transaction():
            role.update(status="ACTIVE")
            self.store.update(role)
        return skill_summary(role)

    def catalog(self):
        return [skill_summary(self.store.get(p["id"], "skill") or p) for p in builtin_people()]

    def avatar(self, rid, data_url=None, reset=False):
        kind = "profile" if rid == "my_profile" else "skill"
        value = self.service.require(rid, kind)
        if reset:
            default = next(
                (p.get("avatar_url", "") for p in builtin_people() if p["id"] == rid), ""
            )
            value["avatar_url"] = default
        else:
            if not isinstance(data_url, str) or len(data_url) > 7_000_000:
                raise DomainError("请选择不超过 5MB 的头像", 413)
            match = re.fullmatch(
                r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)", data_url
            )
            if not match:
                raise DomainError("头像仅支持 PNG、JPEG、WebP")
            try:
                raw = base64.b64decode(match[2], validate=True)
                if len(raw) > 5 * 1024 * 1024:
                    raise DomainError("头像不能超过 5MB", 413)
                with Image.open(io.BytesIO(raw)) as img:
                    if (
                        img.format not in {"PNG", "JPEG", "WEBP"}
                        or img.width * img.height > 25_000_000
                    ):
                        raise DomainError("头像格式或尺寸无效")
                    img = ImageOps.exif_transpose(img).convert("RGB")
                    img = ImageOps.fit(img, (512, 512))
                    img.info.clear()
                    out = io.BytesIO()
                    img.save(out, format="PNG")
                    content = out.getvalue()
            except (
                binascii.Error,
                UnidentifiedImageError,
                OSError,
                Image.DecompressionBombError,
            ) as exc:
                raise DomainError("无法读取该头像，请选择其他图片") from exc
            filename = digest(content.hex()) + ".png"
            (self.media / filename).write_bytes(content)
            value["avatar_url"] = "/user-avatars/" + filename
        with self.store.transaction():
            self.store.update(value)
        return skill_summary(value) if kind == "skill" else value

    def invalidate(self, parent, reason):
        with self.store.transaction():
            for version in self.store.all("user_skill"):
                if parent in version.get("evidence_ids", []) + version.get("directive_ids", []):
                    version.update(
                        status="INVALIDATED", text="[来源已撤回]", summary="", bio="", reason=reason
                    )
                    self.store.update(version)
                    if self.assessments:
                        self.assessments.invalidate(version["id"], reason)
        if self.assessments:
            self.assessments.invalidate(parent, reason)
