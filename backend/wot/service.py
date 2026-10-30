"""Application domain. All admissions originate in trusted human routes, never model fields."""
import json
import re
import threading
from pathlib import Path

from .catalog import builtin_people, role_context, skill_summary
from .dive_adapter import DiveMemoryAdapter
from .domain import DomainError, SourceOrigin, allow_evidence, classify
from .model import Provider
from .storage import AppStore, backup_before_upgrade, canonical, digest, now, operation_context, uid


class WotService:
    def __init__(self, directory: Path, provider: Provider | None = None):
        directory.mkdir(parents=True, exist_ok=True)
        self.upgrade_backup = backup_before_upgrade(directory)
        self.store = AppStore(directory / "wot.db")
        self.core = DiveMemoryAdapter(str(directory / "memory.db"))
        self.provider = provider or Provider()
        self.lock = threading.RLock()
        self.request_locks = {}
        self.invalidate_hooks = []
        self.privacy_revision = 0
        with self.store.transaction():
            if self.store.get("skill_socratic", "skill") is None:
                self.store.add("skill", {
                    "display_name": "苏格拉底式提问者",
                    "description": "借用追问前提的思想方法。解释性虚构角色，并非历史人物本人。",
                    "text": "一次只追问一个关键前提；尊重不同答案，不替用户作决定。",
                    "category": "CURATED", "permission": "项目原创解释性角色",
                    "source": "WOT 原创", "version": 1, "status": "ACTIVE",
                }, rid="skill_socratic")
            # Add missing catalogue entries individually. Existing user edits,
            # custom skills and conversations retain their own stable identities.
            for person in builtin_people():
                if self.store.get(person["id"], "skill") is None:
                    self.store.add("skill", {"status": "ACTIVE", "category": "CURATED",
                                              "version": 1, **person}, rid=person["id"])
            # Earlier versions generated titles from the first human message.
            # Repair old already-deleted sources too, not only future deletions.
            deleted_rooms = self.store.fetchall("SELECT DISTINCT conversation_id FROM messages WHERE owner_id='local' AND deleted=1")
            for row in deleted_rooms:
                conversation = self.store.get(row[0], "conversation")
                if conversation is not None:
                    skill = self.store.get(conversation["skill_id"], "skill")
                    name = skill["display_name"] if skill else "人物"
                    conversation["title"] = f"与{name}的聊天"
                    self.store.update(conversation)
            if deleted_rooms:
                self.store.db.execute("UPDATE requests SET response=''")
        # A generation interrupted by a crash is never silently billed/retried.
        with self.store.transaction():
            for session in self.store.all("simulation"):
                if session["state"] == "GENERATING":
                    session["state"] = "FAILED"
                    session["failure"] = "上次生成中断；不会自动重试"
                    self.store.update(session)
        self.recover()

    def require(self, rid: str, kind: str) -> dict:
        result = self.store.get(rid, kind)
        if result is None:
            raise DomainError("找不到这项本地记录", 404)
        return result

    def once(self, key: str, body: dict, action):
        """Idempotency boundary for local mutations, fingerprint rejects key reuse."""
        if not key or len(key) > 200:
            raise DomainError("需要有效的 Idempotency-Key")
        fingerprint = digest(body)
        with self.lock:
            request_lock = self.request_locks.setdefault(key, threading.RLock())
        with request_lock:
            with self.store.transaction():
                cached_privacy_revision = self.privacy_revision
                row = self.store.fetchone("SELECT * FROM requests WHERE key=?", (key,))
            if row:
                if row["fingerprint"] != fingerprint:
                    raise DomainError("该操作标识已用于不同内容", 409)
                if row["response"]:
                    cached = json.loads(row["response"])
                    with self.store.transaction():
                        if cached_privacy_revision != self.privacy_revision:
                            raise DomainError("资料已撤回，请刷新后查看最新记录", 409)
                        return cached
            else:
                with self.store.transaction():
                    self.store.db.execute("INSERT INTO requests VALUES(?,?,?)", (key, fingerprint, ""))
            context_token = operation_context.set({"key": key, "index": 0})
            privacy_revision = self.privacy_revision
            try:
                result = action()
                with self.store.transaction():
                    own_deletions = operation_context.get().get("privacy_updates", 0)
                    if privacy_revision + own_deletions != self.privacy_revision:
                        raise DomainError("操作期间资料已撤回，请刷新后查看最新记录", 409)
                    self.store.db.execute("UPDATE requests SET response=? WHERE key=?",
                                          (canonical(result), key))
                return result
            finally:
                operation_context.reset(context_token)

    def conversation(self, title: str, skill_id: str | None) -> dict:
        skill = self.require(skill_id or "skill_socratic", "skill")
        if skill["status"] != "ACTIVE":
            raise DomainError("该人物资料当前不可用", 409)
        with self.store.transaction():
            result = self.store.add("conversation", {"title": title, "skill_id": skill["id"]})
        return self.conversation_summary(result)

    def conversation_summary(self, conversation: dict) -> dict:
        last = self.store.fetchone("SELECT rowid,text,created_at,deleted,speaker FROM messages WHERE owner_id='local' "
            "AND conversation_id=? ORDER BY sequence DESC LIMIT 1", (conversation["id"],))
        timestamp = last["created_at"] if last else conversation["created_at"]
        preview = last["text"].replace("\n", " ")[:100] if last else ""
        if last is not None and last["deleted"]:
            preview = "[已删除]" if last["speaker"] == "USER" else "[关联来源已删除]"
        return {**conversation, "updated_at": timestamp,
                "last_message_preview": preview,
                "last_message_at": last["created_at"] if last else None}

    def conversations(self) -> list[dict]:
        summaries = [self.conversation_summary(c) for c in self.store.all("conversation")]
        # Row insertion order breaks equal wall-clock timestamps across rooms.
        activity = {row["conversation_id"]: row["last_rowid"] for row in self.store.fetchall("SELECT conversation_id,MAX(rowid) AS last_rowid FROM messages "
            "WHERE owner_id='local' GROUP BY conversation_id")}
        return sorted(summaries, key=lambda c: (c["updated_at"], activity.get(c["id"], 0),
                                                c["created_at"], c["id"]), reverse=True)

    def messages(self, conversation_id: str) -> list[dict]:
        self.require(conversation_id, "conversation")
        result = []
        for row in self.store.fetchall("SELECT id,conversation_id,speaker,text,created_at,evidence_id,sequence,client_request_id,"
            "deleted,reply_state,request_domain,remember_requested FROM messages "
            "WHERE conversation_id=? AND owner_id='local' ORDER BY sequence", (conversation_id,)):
            message = dict(row)
            domain = message.pop("request_domain")
            remember = message.pop("remember_requested")
            message["deleted"] = bool(message["deleted"])
            if message["deleted"]:
                message["text"] = "[已删除]" if message["speaker"] == "USER" else "[关联来源已删除]"
            message["retry_payload"] = (
                {"text": message["text"], "domain": domain, "remember": bool(remember)}
                if message["speaker"] == "USER" and not message["deleted"]
                and message["reply_state"] in {"WAITING", "FAILED"}
                and domain is not None and remember is not None else None
            )
            result.append(message)
        return result

    def send(self, conversation_id: str, text: str, remember: bool, domain: str, key: str) -> dict:
        conversation = self.require(conversation_id, "conversation")
        # Message IDs survive provider failures: retry reuses the same human turn.
        with self.lock, self.store.transaction():
            old = self.store.fetchone("SELECT * FROM messages WHERE owner_id='local' AND conversation_id=? "
                "AND speaker='USER' AND request_key=?", (conversation_id, key))
            if old:
                if old["deleted"]:
                    raise DomainError("已删除的消息不能重新发送", 409)
                if old["text"] != text or (
                    old["request_domain"] is not None and old["request_domain"] != domain
                ) or (
                    old["remember_requested"] is not None and bool(old["remember_requested"]) != remember
                ):
                    raise DomainError("该消息标识已用于不同内容", 409)
                if old["request_domain"] is None or old["remember_requested"] is None:
                    raise DomainError("旧消息未保留完整请求参数，请另起一条消息", 409)
                user_id = old["id"]
                self.store.db.execute("UPDATE messages SET reply_state='WAITING' WHERE id=?", (user_id,))
            else:
                user_id = self.store.add_message(
                    conversation_id, "USER", text, request_key=key,
                    request_domain=domain, remember_requested=remember, reply_state="WAITING")
                self.store.audit("REAL_MESSAGE_ACCEPTED", user_id, digest(text))
        try:
            evidence = self.admit(user_id, domain) if remember else None
            prior = self.store.fetchone("SELECT * FROM messages WHERE owner_id='local' AND conversation_id=? "
                "AND speaker='AGENT' AND request_key=?", (conversation_id, key))
            if prior is None:
                generation_privacy_revision = self.privacy_revision
                skill = self.require(conversation["skill_id"], "skill")
                if skill["status"] != "ACTIVE":
                    raise DomainError("该人物已停用，历史聊天仍可查看", 409)
                public = [{"speaker": m["speaker"], "text": m["text"]}
                          for m in self.messages(conversation_id)[-12:]]
                reply = self.provider.generate(role="EXTERNAL", topic=text, transcript=public,
                                               private=[], skill=self.skill_context(skill), goals=[])
                with self.store.transaction():
                    current = self.store.fetchone("SELECT deleted FROM messages WHERE id=?", (user_id,))
                    if (generation_privacy_revision != self.privacy_revision or current["deleted"]
                            or self.require(conversation["skill_id"], "skill")["status"] != "ACTIVE"):
                        self.provider.calls.clear()
                        raise DomainError("生成期间消息或人物已撤回，本次回复未发布", 409)
                    agent_id = self.store.add_message(
                        conversation_id, "AGENT", reply["text"], request_key=key)
            else:
                agent_id = prior["id"]
            with self.store.transaction():
                self.store.db.execute("UPDATE messages SET reply_state='REPLIED' WHERE id=?", (user_id,))
            messages = self.messages(conversation_id)
            user = next(m for m in messages if m["id"] == user_id)
            return {"user": user, "agent": next(m for m in messages if m["id"] == agent_id),
                    "evidence_id": evidence["id"] if evidence else user["evidence_id"]}
        except Exception:
            with self.store.transaction():
                self.store.db.execute("UPDATE messages SET reply_state='FAILED' "
                                      "WHERE id=? AND deleted=0", (user_id,))
            raise

    def admit(self, message_id: str, domain: str = "general",
              origin: SourceOrigin = SourceOrigin.REAL_USER_MESSAGE) -> dict:
        with self.lock, self.store.transaction():
            message = self.store.fetchone("SELECT * FROM messages WHERE id=? AND owner_id='local'", (message_id,))
            if message is None or message["speaker"] != "USER":
                raise DomainError("只有本人真聊消息可以纳入真实记忆", 403)
            if message["deleted"] or message["text"] == "[已删除]":
                raise DomainError("已删除的消息不可重新纳入记忆", 409)
            allow_evidence("HUMAN", "local", "local", origin, "ADMIT")
            row = self.store.fetchone("SELECT * FROM evidence WHERE message_id=?", (message_id,))
            if row is not None:
                if row["status"] in {"DELETED", "DELETE_PENDING"}:
                    raise DomainError("已撤回的证据不能恢复", 409)
                eid = row["id"]
            else:
                eid = uid("ev")
                stable_key = digest(["local", message["conversation_id"], message_id,
                                     origin.value])
                self.store.db.execute(
                    "INSERT INTO evidence(id,owner_id,subject_id,message_id,origin,claim_kind,"
                    "domain,text,namespace,stable_key,status,created_at,occurred_at,consent_scope,"
                    "visibility_scope,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, "local", "local", message_id, origin.value,
                     classify(message["text"]), domain, message["text"],
                     "user/local/corrections" if origin == SourceOrigin.USER_CORRECTION else "user/local/real",
                     stable_key, "PENDING", now(), message["created_at"], "MIRROR",
                     "PRIVATE", digest(message["text"])))
                self.store.db.execute("UPDATE messages SET evidence_id=? WHERE id=?", (eid, message_id))
                self.store.db.execute("INSERT INTO outbox(id,evidence_id,operation,state) VALUES(?,?,?,?)",
                                      (uid("out"), eid, "INGEST", "PENDING"))
                self.store.audit("EVIDENCE_ADMISSION_REQUESTED", eid)
        self.recover(eid)
        return self.evidence(eid)

    def evidence(self, eid: str) -> dict:
        row = self.store.fetchone("SELECT * FROM evidence WHERE id=? AND owner_id='local'", (eid,))
        if row is None:
            raise DomainError("找不到证据", 404)
        return dict(row)

    def recover(self, eid: str | None = None) -> None:
        with self.lock:
            query = "SELECT * FROM outbox WHERE state!='APPLIED'"
            rows = self.store.fetchall(query + (" AND evidence_id=?" if eid else ""), (eid,) if eid else ())
            # Delete first: pending ingests must never resurrect a deleted event.
            for row in sorted(rows, key=lambda r: r["operation"] != "DELETE"):
                ev = self.evidence(row["evidence_id"])
                try:
                    if row["operation"] == "DELETE":
                        self.core.delete_by_key(ev["namespace"], ev["stable_key"])
                        with self.store.transaction():
                            self.store.db.execute("UPDATE evidence SET status='DELETED' WHERE id=?",
                                                  (ev["id"],))
                    elif ev["status"] not in {"DELETED", "DELETE_PENDING"}:
                        result = self.core.ingest(ev["namespace"], ev["text"], key=ev["stable_key"],
                                                  message_id=ev["message_id"] or ev["id"],
                                                  observed_at=ev["occurred_at"])
                        with self.store.transaction():
                            self.store.db.execute(
                                "UPDATE evidence SET status='ACTIVE',core_event_id=?,memory_ids=? WHERE id=?",
                                (result["event_id"], canonical(result["memory_ids"]), ev["id"]))
                    with self.store.transaction():
                        self.store.db.execute("UPDATE outbox SET state='APPLIED',error_code=NULL WHERE id=?",
                                              (row["id"],))
                except Exception as exc:
                    # Never persist provider/DB exception text: it may contain private content.
                    with self.store.transaction():
                        self.store.db.execute(
                            "UPDATE outbox SET state='FAILED',attempts=attempts+1,error_code=? WHERE id=?",
                            (type(exc).__name__, row["id"]))
                    if eid:
                        raise DomainError("记忆处理尚未完成，已保存待恢复操作，可稍后重试", 503) from exc

    def active_evidence(self) -> list[dict]:
        return [dict(r) for r in self.store.fetchall("SELECT * FROM evidence WHERE owner_id='local' AND status='ACTIVE' ORDER BY created_at")]

    def mirror(self) -> dict:
        perceived, observed = [], []
        for ev in self.active_evidence():
            if ev["claim_kind"] not in {"SELF_REPORT", "OBSERVATION"}:
                continue
            trait = {"id": "trait_" + ev["id"], "proposition": ev["text"], "status": "HYPOTHESIS",
                     "supports": [ev["id"]], "counters": [], "domain": ev["domain"],
                     "uncertainty": "单条自述，只作观察线索；未验证跨情境稳定性。"}
            if ev["claim_kind"] == "SELF_REPORT":
                trait["status"] = "SELF_REPORTED"
                perceived.append(trait)
            else:
                observed.append(trait)
        return {"PERCEIVED": perceived, "OBSERVED": observed,
                "CHOSEN": [d for d in self.store.all("directive") if d["state"] == "APPROVED"]}

    def propose(self, objective: str, domain: str, limits: str) -> dict:
        with self.store.transaction():
            result = self.store.add("directive", {"objective": objective, "domain": domain,
                                                 "limits": limits, "state": "PROPOSED", "revision": 1,
                                                 "origin": "USER_CHOSEN_DIRECTIVE"})
            self.store.audit("CHOSEN_GOAL_PROPOSED", result["id"])
            return result

    def directive(self, rid: str, revision: int, operation: str) -> dict:
        with self.lock, self.store.transaction():
            result = self.require(rid, "directive")
            target = "APPROVED" if operation == "approve" else "REVOKED"
            if result["state"] == target and revision in {result["revision"], result["revision"] - 1}:
                return result
            if result["revision"] != revision:
                raise DomainError("目标版本已改变，请刷新后确认", 409)
            if result["state"] == "REVOKED":
                raise DomainError("已撤回目标不能直接重新启用，请新建提案", 409)
            result.update(state=target, revision=revision + 1, approval_at=now())
            self.store.update(result)
            self.store.audit("CHOSEN_GOAL_" + target, rid, {"revision": revision})
            if target == "REVOKED":
                self.invalidate(rid, "目标已由本人撤回")
                for action in self.store.all("growth"):
                    if action["directive_id"] == rid:
                        action["state"] = "REVOKED"
                        self.store.update(action)
            return result

    def compile(self, track: str) -> dict:
        track = track.upper()
        if track not in {"PERCEIVED", "OBSERVED", "CHOSEN"}:
            raise DomainError("无效的自我轨道")
        evidence_ids = []
        if track != "CHOSEN":
            evidence_ids = [eid for t in self.mirror()[track] for eid in t["supports"]]
        directives = [d for d in self.store.all("directive") if d["state"] == "APPROVED"]
        directive_ids = [d["id"] for d in directives] if track == "CHOSEN" else []
        evidence_manifest = [{"id": eid, "hash": self.evidence(eid)["content_hash"],
                              "origin": self.evidence(eid)["origin"],
                              "at": self.evidence(eid)["occurred_at"]} for eid in evidence_ids]
        model_config = {"provider": self.provider.name, "model": self.provider.model, "temperature": 0.4}
        fingerprint = digest({"track": track, "evidence": evidence_manifest, "goals": directive_ids,
                              "policy": "conservative-reference-v1", "model": model_config})
        for version in self.store.all("version"):
            if version["snapshot_hash"] == fingerprint and version["state"] == "ACTIVE":
                return version
        with self.store.transaction():
            version = self.store.add("version", {
                "track": track, "state": "ACTIVE", "evidence_ids": evidence_ids,
                "directive_ids": directive_ids, "snapshot_hash": fingerprint,
                "compiler_version": "conservative-reference-v1", "model": self.provider.model,
                "model_config_hash": digest(model_config), "evidence_manifest_hash": digest(evidence_manifest),
                "mode": "EXPERIMENTAL_PREVIEW", "invalidated_reason": None,
            }, rid="version_" + fingerprint)
            for parent in evidence_ids + directive_ids:
                self.store.link(parent, version["id"])
            self.store.audit("PERSONA_VERSION_COMPILED", version["id"], fingerprint)
            return version

    def invalidate(self, parent: str, reason: str) -> None:
        for row in self.store.fetchall("SELECT child_id FROM dependencies WHERE parent_id=?", (parent,)):
            version = self.store.get(row[0], "version")
            if version:
                version.update(state="INVALIDATED", invalidated_reason=reason)
                self.store.update(version)
                self.store.audit("PERSONA_VERSION_INVALIDATED", version["id"])
                for session in self.store.all("simulation"):
                    if session["persona_version_id"] == version["id"]:
                        session.update(state="INVALIDATED", failure=reason)
                        self.store.update(session)
                        for turn in self.store.all("turn"):
                            if turn["session_id"] == session["id"]:
                                turn.update(text="[来源已撤回，模拟内容已清除]", evidence_refs=[])
                                self.store.update(turn)
        for callback in self.invalidate_hooks:
            callback(parent, reason)

    def delete(self, eid: str) -> dict:
        with self.lock, self.store.transaction():
            ev = self.evidence(eid)
            if ev["status"] == "DELETED":
                return {"id": eid, "status": "DELETED"}
            # Fail closed before touching core, including a crash between DBs.
            self.store.db.execute("UPDATE evidence SET status='DELETE_PENDING',text='' WHERE id=?", (eid,))
            self.store.db.execute("UPDATE messages SET text='[已删除]',deleted=1 WHERE id=?", (ev["message_id"],))
            msg = self.store.fetchone("SELECT conversation_id FROM messages WHERE id=?", (ev["message_id"],))
            if msg:
                conversation = self.store.get(msg["conversation_id"], "conversation")
                if conversation is not None:
                    skill = self.store.get(conversation["skill_id"], "skill")
                    conversation["title"] = f"与{skill['display_name'] if skill else '人物'}的聊天"
                    self.store.update(conversation)
                self.store.db.execute("UPDATE messages SET text='[关联来源已删除]',deleted=1 WHERE conversation_id=? AND speaker='AGENT'",
                                      (msg[0],))
            self.invalidate(eid, "依赖的真实证据已删除")
            self.privacy_revision += 1
            operation = operation_context.get()
            if operation is not None:
                operation["privacy_updates"] = operation.get("privacy_updates", 0) + 1
            self.store.db.execute("INSERT OR IGNORE INTO outbox(id,evidence_id,operation,state) VALUES(?,?,?,?)",
                                  (uid("out"), eid, "DELETE", "PENDING"))
            # Cached responses may contain original text; never serve them again.
            self.store.db.execute("UPDATE requests SET response=''")
            self.provider.calls.clear()
            self.store.audit("EVIDENCE_DELETED", eid)
        self.recover(eid)
        return {"id": eid, "status": self.evidence(eid)["status"]}

    def correct(self, eid: str, text: str, domain: str) -> dict:
        with self.lock:
            ev = self.evidence(eid)
            if ev["message_id"] is None and hasattr(self, "people"):
                self.delete(eid)
                return self.people.statement(text, "USER_CORRECTION", domain)
            msg = self.store.fetchone("SELECT * FROM messages WHERE id=?", (ev["message_id"],))
            operation = operation_context.get()
            operation_id = "correction_" + digest([eid, operation["key"] if operation else text])[:32]
            pending = self.store.get(operation_id, "correction")
            if pending is None:
                if ev["status"] != "ACTIVE":
                    raise DomainError("只有有效证据可以更正", 409)
                with self.store.transaction():
                    pending = self.store.add("correction", {"evidence_id": eid, "message_id": uid("msg"),
                                                            "content_hash": digest(text)}, rid=operation_id)
            if pending["content_hash"] != digest(text):
                raise DomainError("该更正操作的内容已经改变", 409)
            self.delete(eid)
            mid = pending["message_id"]
            with self.store.transaction():
                if self.store.fetchone("SELECT id FROM messages WHERE id=?", (mid,)) is None:
                    self.store.add_message(
                        msg["conversation_id"], "USER", text, request_key=operation_id,
                        request_domain=domain, remember_requested=True, message_id=mid)
                self.store.audit("USER_CORRECTION", mid, {"supersedes": eid})
            return self.admit(mid, domain, SourceOrigin.USER_CORRECTION)

    def import_skill(self, text: str, category: str, permission: str, source: str) -> dict:
        if hasattr(self, "people"):
            return self.people.draft({"text":text,"category":category,"permission":permission,"source":source})
        if category not in {"CURATED", "CONSENTED", "PRIVATE"} or not permission.strip() or not source.strip():
            raise DomainError("请选择角色类型并说明来源与使用授权")
        if len(text.encode("utf-8")) > 64_000:
            raise DomainError("角色资料超过 64 KB 限制", 413)
        if "\x00" in text:
            raise DomainError("只接受普通文本角色资料")
        metadata = {}
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) != 3:
                raise DomainError("角色资料的元信息格式不完整")
            for line in parts[1].splitlines():
                match = re.fullmatch(r"(name|description|version|license|author):\s*(.{1,500})", line)
                if match:
                    metadata[match[1]] = match[2].strip("\"'")
        name = metadata.get("name", "我的解释性角色")
        with self.store.transaction():
            result = self.store.add("skill", {
                "display_name": name, "description": metadata.get("description", "本地静态文本角色，AI 模拟"),
                "text": text, "category": category, "permission": permission, "source": source,
                "version": metadata.get("version", "1"), "status": "ACTIVE", "content_hash": digest(text),
            })
            self.store.audit("SKILL_IMPORTED_AS_DATA", result["id"], result["content_hash"])
            return result

    @staticmethod
    def skill_context(skill: dict) -> dict:
        return role_context(skill)

    def simulation(self, topic: str, version_id: str, skill_id: str, max_turns: int,
                   allow_private_context: bool = False, max_tokens: int = 6000) -> dict:
        if hasattr(self, "assessments"):
            self.assessments.assert_unlocked()
            raise DomainError("旧预览记录仅供回看，请在虚拟空间使用已校验分身新建会话", 409)
        version = self.require(version_id, "version")
        skill = self.require(skill_id, "skill")
        if version["state"] != "ACTIVE" or version["track"] not in {"OBSERVED", "CHOSEN"}:
            raise DomainError("请选择有效的观察我或理想我版本", 409)
        if skill["status"] != "ACTIVE":
            raise DomainError("该角色已停用", 409)
        with self.store.transaction():
            result = self.store.add("simulation", {
                "topic": topic, "persona_version_id": version_id, "skill_id": skill_id,
                "skill_hash": digest(self.skill_context(skill)), "state": "CREATED", "max_turns": max_turns,
                "max_tokens": max_tokens, "used_tokens": 0, "mode": "EXPERIMENTAL_PREVIEW",
                "disclosed_as_ai": True, "allow_private_context": allow_private_context,
                "provider": self.provider.name, "model": self.provider.model, "calls": 0,
                "disclosure": "AI 模拟，非真实对话", "failure": None,
            })
            self.store.audit("SIMULATION_CREATED", result["id"], {"granted": allow_private_context})
            return result

    def session_view(self, rid: str) -> dict:
        session = self.require(rid, "simulation")
        turns = sorted((t for t in self.store.all("turn") if t["session_id"] == rid), key=lambda t: t["turn_index"])
        ids = {t["id"] for t in turns}
        return {**session, "turns": turns,
                "feedback": [f for f in self.store.all("feedback") if f["turn_id"] in ids]}

    def next_turn(self, rid: str, key: str, disclosure_approved: bool = False) -> dict:
        if hasattr(self, "assessments"):
            self.assessments.assert_unlocked()
            raise DomainError("旧预览记录仅供回看，请在虚拟空间新建会话", 409)
        # Hold the serial generation lock. Pause uses its own short DB transaction,
        # so it remains responsive while a provider request is in flight.
        with self.lock:
            session = self.require(rid, "simulation")
            previous = next((t for t in self.store.all("turn") if t.get("request_key") == key
                             and t["session_id"] == rid), None)
            if previous:
                return self.session_view(rid)
            if session["state"] not in {"CREATED", "RUNNING"}:
                raise DomainError("模拟当前不能生成；请检查暂停或失效状态", 409)
            version = self.require(session["persona_version_id"], "version")
            if version["state"] != "ACTIVE":
                raise DomainError("该版本已失效，请重新编译", 409)
            turns = self.session_view(rid)["turns"]
            if len(turns) >= session["max_turns"] or session["used_tokens"] >= session["max_tokens"]:
                with self.store.transaction():
                    session["state"] = "COMPLETED"
                    self.store.update(session)
                raise DomainError("已达到本次模拟的回合或用量上限", 409)
            if session["provider"] != self.provider.name or session["model"] != self.provider.model:
                raise DomainError("模型配置已变化，请新建模拟", 409)
            role = "PERSONA" if len(turns) % 2 == 0 else "EXTERNAL"
            if role == "EXTERNAL" and not disclosure_approved:
                raise DomainError("请先检查分身上一句，并明确允许人物读取后再继续", 403)
            skill = self.require(session["skill_id"], "skill")
            if digest(self.skill_context(skill)) != session["skill_hash"]:
                raise DomainError("角色资料已变化，请新建模拟", 409)
            private, goals = [], []
            if role == "PERSONA":
                if version["track"] == "CHOSEN":
                    goals = [{"objective": self.require(did, "directive")["objective"],
                              "limits": self.require(did, "directive")["limits"]}
                             for did in version["directive_ids"]]
                if session["allow_private_context"]:
                    allowed = set(version["evidence_ids"])
                    namespaces = {e["namespace"] for e in self.active_evidence() if e["id"] in allowed}
                    ranked = [item for namespace in sorted(namespaces)
                              for item in self.core.retrieve(namespace, session["topic"], limit=12)]
                    source_ids = {event for item in ranked for event in item.get("event_ids", [])}
                    for ev in self.active_evidence():
                        if ev["id"] in allowed and ev["core_event_id"] in source_ids:
                            allow_evidence("PERSONA", "local", ev["subject_id"],
                                           SourceOrigin(ev["origin"]), "SIMULATION", True)
                            private.append({"id": ev["id"], "text": ev["text"][:1000],
                                            "kind": ev["claim_kind"]})
                            if len(private) == 3:
                                break
            # No reference list or private text is copied into external role context.
            public = [{"speaker": t["speaker"], "text": t["text"]} for t in turns[-12:]]
            # UTF-8 bytes bound the serialized data tokens conservatively. Reserve
            # system-prompt overhead + output before a potentially paid request.
            reserve = len(canonical({"topic": session["topic"], "transcript": public,
                                     "private": private, "goals": goals,
                                     "skill": self.skill_context(skill) if role == "EXTERNAL" else {}}).encode()) + 1600
            if self.provider.name == "mock":
                reserve = 400  # local fixed templates, no provider input tokens
            if session["used_tokens"] + reserve > session["max_tokens"]:
                with self.store.transaction():
                    session.update(state="COMPLETED", failure="剩余用量不足以安全生成下一轮")
                    self.store.update(session)
                raise DomainError("剩余用量不足，本次模拟已停止", 409)
            with self.store.transaction():
                session.update(state="GENERATING", calls=session["calls"] + 1,
                               used_tokens=session["used_tokens"] + reserve)
                self.store.update(session)
            try:
                reply = self.provider.generate(role=role, topic=session["topic"], transcript=public,
                                               private=private, goals=goals,
                                               skill=self.skill_context(skill) if role == "EXTERNAL" else {})
                # Server-side disclosure guard: avoid verbatim private excerpts and IDs.
                text = reply["text"]
                for ev in private:
                    if ev["text"] in text or ev["id"] in text:
                        raise DomainError("生成内容触及私人引用，已阻止该回合", 502)
            except Exception:
                with self.store.transaction():
                    current = self.require(rid, "simulation")
                    if current["state"] == "GENERATING":
                        current.update(state="FAILED", failure="本轮生成失败；未保存虚构的成功回合")
                        self.store.update(current)
                raise
            with self.store.transaction():
                current = self.require(rid, "simulation")
                if current["state"] != "GENERATING":
                    return self.session_view(rid)  # paused/cancelled while request was in flight
                self.store.add("turn", {"session_id": rid, "speaker": role, "text": text,
                                        "turn_index": len(turns), "is_generated": True,
                                        "origin": "SIMULATED_DIALOGUE", "request_key": key,
                                        "evidence_refs": [e["id"] for e in private], "usage": reply["tokens"],
                                        "disclosure": "AI 模拟，非真实对话"})
                current.update(state="COMPLETED" if len(turns) + 1 == session["max_turns"] else "RUNNING",
                               used_tokens=session["used_tokens"] - reserve + reply["tokens"])
                self.store.update(current)
                self.store.audit("SIMULATION_TURN_COMMITTED", rid, {"index": len(turns)}, actor=role)
            return self.session_view(rid)

    def control(self, rid: str, action: str) -> dict:
        if action == "resume" and hasattr(self, "assessments"):
            self.assessments.assert_unlocked()
            raise DomainError("旧预览记录仅供回看，不能继续生成", 409)
        with self.store.transaction():
            session = self.require(rid, "simulation")
            if session["state"] in {"INVALIDATED", "COMPLETED", "CANCELLED"}:
                raise DomainError("此模拟已经结束或失效", 409)
            if action == "resume" and session["state"] not in {"PAUSED", "FAILED"}:
                raise DomainError("当前无需继续", 409)
            session["state"] = {"pause": "PAUSED", "resume": "RUNNING", "cancel": "CANCELLED"}[action]
            self.store.update(session)
            self.store.audit("SIMULATION_" + action.upper(), rid)
        return self.session_view(rid)

    def feedback(self, turn_id: str, verdict: str, comment: str, intent_scope: str) -> dict:
        self.require(turn_id, "turn")
        if intent_scope not in {"SIM_STYLE_CORRECTION", "SIM_VALUE_DISAGREEMENT",
                                 "NEW_REAL_USER_STATEMENT", "CHOSEN_GOAL_AMENDMENT"}:
            raise DomainError("无效的反馈用途")
        with self.store.transaction():
            return self.store.add("feedback", {"turn_id": turn_id, "verdict": verdict,
                                               "comment": comment, "intent_scope": intent_scope,
                                               "origin": "SIMULATION_FEEDBACK", "review_state": "PROPOSED"})

    def growth(self, directive_id: str, title: str, trigger: str, action: str,
               user_approved: bool) -> dict:
        directive = self.require(directive_id, "directive")
        if directive["state"] != "APPROVED" or not user_approved:
            raise DomainError("请先确认目标，并由本人批准这次小行动", 403)
        with self.store.transaction():
            return self.store.add("growth", {"directive_id": directive_id, "title": title,
                                             "trigger": trigger, "action": action, "state": "APPROVED",
                                             "outcome": None, "user_approved_at": now()})

    def checkin(self, rid: str, outcome: str | None) -> dict:
        with self.store.transaction():
            action = self.require(rid, "growth")
            if action["state"] == "REVOKED":
                raise DomainError("该行动已撤回", 409)
            action.update(state="REVOKED" if outcome is None else "SELF_REPORTED",
                          outcome=outcome, origin="USER_SELF_REPORT")
            self.store.update(action)
            self.store.audit("GROWTH_REVOKED" if outcome is None else "GROWTH_SELF_REPORTED", rid)
            return action

    def state(self) -> dict:
        with self.lock:
            return {"conversations": self.conversations(), "messages": [],
                    "evidence": self.active_evidence(), "mirror": self.mirror(),
                    "directives": self.store.all("directive"), "versions": self.store.all("version"),
                    "skills": [skill_summary(s) for s in self.store.all("skill")],
                    "simulations": [self.session_view(s["id"]) for s in self.store.all("simulation")],
                    "growth": self.store.all("growth"), "privacy": {
                        "provider": self.provider.name, "model": self.provider.model,
                        "external_calls": self.provider.external_calls, "telemetry": False,
                        "external_fields": ["当前公开对话", "角色资料", "所选已批准目标",
                                            "仅主动授权后：最多三项主题相关私人证据"],
                    }, "stage": "LOCAL_RESEARCH_PREVIEW"}

    def export(self) -> dict:
        data = self.state()
        # Ordinary sidebar APIs omit runtime text; the local owner can still
        # explicitly export complete role materials under their existing rights.
        data["skills"] = self.store.all("skill")
        data["messages"] = [m for c in data["conversations"] for m in self.messages(c["id"])]
        return {"format": "wot-export-v1", "exported_at": now(),
                "disclosure": "模拟空间的所有内容均为 AI 模拟，非真实对话。已下载副本无法由应用撤回。",
                **data}

    def close(self):
        self.core.close()
        self.store.db.close()
