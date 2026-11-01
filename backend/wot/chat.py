"""Durable ordinary-chat bursts. Only published parts are ordinary messages."""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .domain import DomainError
from .storage import digest

ACTIVE = {"QUEUED", "GENERATING", "DELIVERING", "WAITING_FOLLOWUP"}
RETRYABLE = {"FAILED", "INTERRUPTED"}


class ChatEngine:
    def __init__(self, service, *, debounce=0.8, part_interval=(1, 4), idle_interval=(20, 35),
                 presence_seconds=10, clock=None):
        self.service, self.store = service, service.store
        self.lock = service.lock
        self.debounce = debounce
        self.part_interval, self.idle_interval = part_interval, idle_interval
        self.presence_seconds = presence_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wot-chat")
        self.inflight: set[str] = set()
        self.presences: dict[str, dict] = {}
        self.stop_event = threading.Event()
        self.started = False
        self.closed = False
        with self.lock, self.store.transaction():
            for batch in self.store.all("reply_batch"):
                if batch["status"] in ACTIVE:
                    if batch["status"] == "WAITING_FOLLOWUP":
                        batch.update(status="COMPLETED", idle_followup=None, next_at=None)
                        self._save(batch)
                        continue
                    batch.update(status="INTERRUPTED", epoch=batch["epoch"] + 1,
                                 idle_followup=None, privacy_revision=service.privacy_revision,
                                 failure="服务重新启动，回复已停止；可手动重试")
                    self._save(batch)
                    self._user_state(batch, "FAILED")
        service.invalidate_hooks.append(self.invalidate)

    def _at(self, seconds=0):
        return (self.clock() + timedelta(seconds=seconds)).isoformat()

    def _save(self, batch):
        conversation = self.service.require(batch["conversation_id"], "conversation")
        conversation["reply_revision"] = conversation.get("reply_revision", 0) + 1
        self.store.update(conversation)
        batch["revision"] = conversation["reply_revision"]
        self.store.update(batch)

    def _latest(self, conversation_id):
        conversation = self.service.require(conversation_id, "conversation")
        rid = conversation.get("reply_batch_id")
        return self.store.get(rid, "reply_batch") if rid else None

    def public(self, batch):
        if not batch:
            return None
        research = getattr(self.service, "research", None)
        progress = research.progress(batch["conversation_id"]) if research else None
        if progress and not set(batch["source_message_ids"]).intersection(progress.get("source_message_ids", [])):
            progress = None
        return {key: batch.get(key) for key in (
            "id", "conversation_id", "status", "failure", "revision", "published_count",
            "source_message_ids",
        )} | {"typing": batch["status"] in {"QUEUED", "GENERATING", "DELIVERING"},
               "research_phase": progress.get("phase") if progress and batch["status"] == "GENERATING" else None,
               "research_status": progress.get("status") if progress else None,
               "research_notice": progress.get("notice") if progress else None}

    def web(self, conversation_id, enabled):
        if type(enabled) is not bool:
            raise DomainError("联网开关无效")
        with self.lock, self.store.transaction():
            conversation = self.service.require(conversation_id, "conversation")
            if conversation.get("web_enabled", True) != enabled:
                conversation["web_enabled"] = enabled
                self.store.update(conversation)
                batch = self._latest(conversation_id)
                if batch and batch["status"] in ACTIVE:
                    self._cancel(batch, "联网设置已改变，请重新发送或继续聊天")
                    self._user_state(batch, "FAILED" if not batch["published_count"] else "REPLIED")
                research = getattr(self.service, "research", None)
                if research:
                    research.invalidate_scope(conversation_id, "联网设置已改变", pending_only=True)
            return self.service.conversation_summary(conversation)

    def _user_state(self, batch, state):
        for mid in batch["source_message_ids"]:
            self.store.db.execute("UPDATE messages SET reply_state=? WHERE id=? AND deleted=0",
                                  (state, mid))

    def _valid(self, batch):
        research = getattr(self.service, "research", None)
        if research and batch.get("research_id") and research.stale(batch["research_id"]):
            raise DomainError("联网设置或资料已改变，本次未发内容已停止", 409)
        if batch.get("model_hash") and hasattr(self.service.provider, "config_hash"):
            if self.service.provider.config_hash(batch.get("connection_id")) != batch["model_hash"]:
                raise DomainError("模型配置已改变，本次未发内容已停止", 409)
        if batch["privacy_revision"] != self.service.privacy_revision:
            raise DomainError("资料已撤回，本次回复未发布", 409)
        skill = self.service.require(batch["skill_id"], "skill")
        if skill["status"] != "ACTIVE" or digest({"text": skill["text"], "version": skill.get("version", 1)}) != batch["skill_version"]:
            raise DomainError("人物资料已停用或更新，本次回复未发布", 409)
        for mid in batch["source_message_ids"]:
            source = self.store.fetchone("SELECT deleted FROM messages WHERE id=? AND owner_id='local'", (mid,))
            if not source or source["deleted"]:
                raise DomainError("来源消息已撤回，本次回复未发布", 409)

    def _cancel(self, batch, reason, *, purge=True):
        batch.update(status="CANCELLED", epoch=batch["epoch"] + 1,
                     failure=reason, idle_followup=None, next_at=None)
        if purge:
            batch["parts"] = []
        self.service.agents.pause_scope(batch["conversation_id"], reason, clear_pending=True)
        self._save(batch)

    def accept(self, conversation_id, text, remember, domain, key, *, immediate=False):
        if not key or len(key) > 200:
            raise DomainError("发送消息需要有效的 Idempotency-Key")
        with self.lock, self.store.transaction():
            conversation = self.service.require(conversation_id, "conversation")
            skill = self.service.require(conversation["skill_id"], "skill")
            if skill["status"] != "ACTIVE":
                raise DomainError("该人物已停用，历史聊天仍可查看", 409)
            old = self.store.fetchone("SELECT * FROM messages WHERE owner_id='local' AND conversation_id=? "
                                      "AND speaker='USER' AND request_key=?", (conversation_id, key))
            if old:
                if old["deleted"]:
                    raise DomainError("已删除的消息不能重新发送", 409)
                if old["text"] != text or old["request_domain"] != domain or old["remember_requested"] is None or bool(old["remember_requested"]) != remember:
                    raise DomainError("该消息标识已用于不同内容或未保留完整旧参数", 409)
                batch = self.store.get(old["reply_batch_id"], "reply_batch") if old["reply_batch_id"] else None
                if batch:
                    return {"user": next(m for m in self.service.messages(conversation_id) if m["id"] == old["id"]),
                            "batch": self.public(batch)}
                prior = self.store.fetchall("SELECT id FROM messages WHERE owner_id='local' AND conversation_id=? "
                                            "AND speaker='AGENT' AND request_key=? ORDER BY sequence", (conversation_id, key))
                if prior:
                    # Adopt a pre-batch receipt without regenerating or changing
                    # historical content, IDs, order or its original consent.
                    batch = self.store.add("reply_batch", {
                        "conversation_id": conversation_id, "skill_id": skill["id"],
                        "skill_version": digest({"text": skill["text"], "version": skill.get("version", 1)}),
                        "source_message_ids": [old["id"]], "request_key": key,
                        "privacy_revision": self.service.privacy_revision, "context_sequence": old["sequence"],
                        "status": "COMPLETED", "epoch": 0, "parts": [], "generated": True,
                        "published_count": len(prior), "next_index": 0, "idle_followup": None,
                        "idle_cancelled": True, "next_at": None, "failure": None, "generation_id": None,
                    })
                    self.store.db.execute("UPDATE messages SET reply_batch_id=?,reply_state='REPLIED' WHERE id=?",
                                          (batch["id"], old["id"]))
                    for index, row in enumerate(prior):
                        self.store.db.execute("UPDATE messages SET reply_batch_id=?,part_index=? WHERE id=?",
                                              (batch["id"], index, row["id"]))
                    if not conversation.get("reply_batch_id"):
                        conversation["reply_batch_id"] = batch["id"]
                        self.store.update(conversation)
                    self._save(batch)
                    return {"user": next(m for m in self.service.messages(conversation_id) if m["id"] == old["id"]),
                            "batch": self.public(batch)}
                # Legacy failed turns retain their original identity and consent.
                user_id = old["id"]
                self.store.db.execute("UPDATE messages SET reply_state='WAITING' WHERE id=?", (user_id,))
            else:
                user_id = self.store.add_message(conversation_id, "USER", text, request_key=key,
                                                request_domain=domain, remember_requested=remember,
                                                reply_state="WAITING")
                self.store.audit("REAL_MESSAGE_ACCEPTED", user_id, digest(text))
            previous = self._latest(conversation_id)
            if previous and previous["status"] in ACTIVE | RETRYABLE:
                self._cancel(previous, "新的消息已到达，未发内容已取消")
            waiting = self.store.fetchall("SELECT id FROM messages WHERE owner_id='local' AND conversation_id=? "
                                          "AND speaker='USER' AND deleted=0 AND reply_state='WAITING' ORDER BY sequence",
                                          (conversation_id,))
            source_ids = [row["id"] for row in waiting]
            batch = self.store.add("reply_batch", {
                "conversation_id": conversation_id, "skill_id": skill["id"],
                "skill_version": digest({"text": skill["text"], "version": skill.get("version", 1)}),
                "source_message_ids": source_ids, "request_key": key,
                "privacy_revision": self.service.privacy_revision, "context_sequence": None,
                "status": "QUEUED", "epoch": 0, "parts": [], "generated": False,
                "published_count": 0, "next_index": 0, "idle_followup": None,
                "idle_cancelled": False,
                "next_at": self._at(0 if immediate else self.debounce),
                "failure": None, "generation_id": None,
            })
            conversation = self.service.require(conversation_id, "conversation")
            conversation["reply_batch_id"] = batch["id"]
            self.store.update(conversation)
            for mid in source_ids:
                self.store.db.execute("UPDATE messages SET reply_batch_id=? WHERE id=?", (batch["id"], mid))
            self._save(batch)
        try:
            if remember:
                self.service.admit(user_id, domain)
        except Exception:
            with self.lock, self.store.transaction():
                batch = self.service.require(batch["id"], "reply_batch")
                if batch["status"] != "CANCELLED":
                    batch.update(status="FAILED", epoch=batch["epoch"] + 1,
                                 failure="消息已保存，记忆处理未完成；请稍后重试")
                    self._user_state(batch, "FAILED")
                    self._save(batch)
            raise
        return {"user": next(m for m in self.service.messages(conversation_id) if m["id"] == user_id),
                "batch": self.public(batch)}

    def retry(self, rid):
        # Re-run only the independently authorized memory admission, not a model.
        # An interrupted outbox must recover before this turn is regenerated.
        with self.lock:
            initial = self.service.require(rid, "reply_batch")
            if initial["status"] in RETRYABLE:
                self._valid(initial)
                sources = [self.store.fetchone("SELECT * FROM messages WHERE id=?", (mid,))
                           for mid in initial["source_message_ids"]]
            else:
                sources = []
        for source in sources:
            if source["remember_requested"]:
                self.service.admit(source["id"], source["request_domain"])
        with self.lock, self.store.transaction():
            batch = self.service.require(rid, "reply_batch")
            self._valid(batch)
            if batch["status"] in ACTIVE or batch["status"] == "COMPLETED":
                return {"batch": self.public(batch)}
            if batch["status"] not in RETRYABLE or self._latest(batch["conversation_id"])["id"] != rid:
                raise DomainError("这次回复已被新消息取代，不能继续", 409)
            batch.update(status="DELIVERING" if batch["generated"] else "QUEUED",
                         epoch=batch["epoch"] + 1, failure=None, next_at=self._at(), idle_followup=None)
            self._user_state(batch, "WAITING")
            self._save(batch)
            return {"batch": self.public(batch)}

    def presence(self, data):
        if set(data) - {"conversation_id", "tab_id", "visible", "composing"}:
            raise DomainError("活跃状态包含未知字段")
        cid, tab = data.get("conversation_id"), data.get("tab_id")
        if not isinstance(tab, str) or not tab or len(tab) > 200 or type(data.get("visible")) is not bool or type(data.get("composing")) is not bool:
            raise DomainError("活跃状态无效")
        if cid is not None:
            self.service.require(cid, "conversation")
        with self.lock, self.store.transaction():
            old = self.presences.get(tab)
            self.presences[tab] = {**data, "expires_at": self._at(self.presence_seconds)}
            for target in {cid, old.get("conversation_id") if old else None} - {None}:
                batch = self._latest(target)
                if batch and batch["status"] in ACTIVE and (data["composing"] or not self._present(target)):
                    self._drop_idle(batch)
        return {"ok": True}

    def _present(self, cid):
        return any(p["conversation_id"] == cid and p["visible"] and not p["composing"]
                   and p["expires_at"] > self._at() for p in self.presences.values())

    def _drop_idle(self, batch):
        self.service.agents.acknowledge(batch.get("agent_id"), len(batch["parts"]), batch.get("generation_id"))
        batch["idle_followup"] = None
        batch["idle_cancelled"] = True
        if batch["status"] == "WAITING_FOLLOWUP":
            batch.update(status="COMPLETED", next_at=None)
        self._save(batch)

    def updates(self, conversation_id, after_sequence=0, after_revision=0):
        if after_sequence < 0 or after_revision < 0:
            raise DomainError("消息游标无效")
        with self.lock:
            conversation = self.service.require(conversation_id, "conversation")
            latest = self._latest(conversation_id)
            reset = after_revision < conversation.get("redaction_revision", 0)
            messages = self.service.messages(conversation_id)
            return {"messages": messages if reset else [m for m in messages if m["sequence"] > after_sequence],
                    "batch": self.public(latest), "revision": conversation.get("reply_revision", 0),
                    "reset": reset}

    def _delay(self, text):
        lo, hi = self.part_interval
        return min(hi, max(lo, lo + len(text) / 90))

    def _publish(self, batch):
        self._valid(batch)
        index = batch["next_index"]
        idle = batch["status"] == "WAITING_FOLLOWUP"
        if not idle and index >= len(batch["parts"]):
            batch.update(status="COMPLETED", next_at=None, idle_followup=None)
            self._user_state(batch, "REPLIED")
            self._save(batch)
            return
        text = batch["idle_followup"] if idle else batch["parts"][index]
        if not text:
            batch.update(status="COMPLETED", next_at=None)
            self._save(batch)
            return
        part_index = len(batch["parts"]) if idle else index
        part_key = "part_" + digest([batch["id"], part_index])
        message_id = "msg_" + digest([batch["id"], part_index])[:32]
        self.store.add_message(batch["conversation_id"], "AGENT", text,
                               request_key=part_key, message_id=message_id,
                               reply_batch_id=batch["id"], part_index=part_index,
                               client_request_id=batch["request_key"])
        research = getattr(self.service, "research", None)
        if research and batch.get("research_id"):
            metadata = research.publish(batch["research_id"], message_id, part_index, text)
            self.store.db.execute("UPDATE messages SET research_id=?,source_refs=? WHERE id=?",
                                  (metadata.get("research_id"), json.dumps(metadata.get("source_refs", [])), message_id))
        self.service.agents.acknowledge(batch.get("agent_id"), part_index, batch.get("generation_id"))
        batch["published_count"] += 1
        if not idle:
            batch["next_index"] += 1
        if not idle and batch["next_index"] < len(batch["parts"]):
            batch.update(status="DELIVERING", next_at=self._at(self._delay(batch["parts"][batch["next_index"]])))
        else:
            self._user_state(batch, "REPLIED")
            if not idle and batch.get("idle_followup") and self._present(batch["conversation_id"]):
                batch.update(status="WAITING_FOLLOWUP", next_at=self._at(random.uniform(*self.idle_interval)))
            else:
                batch.update(status="COMPLETED", next_at=None, idle_followup=None)
        self._save(batch)

    def _cancelled(self, rid, epoch):
        if self.stop_event.is_set():
            return True
        batch = self.store.get(rid, "reply_batch")
        if not batch or batch["epoch"] != epoch or batch["status"] != "GENERATING":
            return True
        try:
            self._valid(batch)
        except DomainError:
            return True
        return False

    def _generate(self, rid, epoch):
        try:
            with self.lock, self.store.transaction():
                batch = self.service.require(rid, "reply_batch")
                if self._cancelled(rid, epoch):
                    return
                skill = self.service.require(batch["skill_id"], "skill")
                messages = self.service.messages(batch["conversation_id"])
                batch["context_sequence"] = messages[-1]["sequence"] if messages else 0
                connection = self.service.provider.active() if hasattr(self.service.provider, "active") else None
                if connection:
                    batch.update(connection_id=connection["id"],
                                 model_hash=self.service.provider.config_hash(connection["id"]))
                public = [{"id": m["id"], "speaker": m["speaker"], "text": m["text"],
                           "speaker_id": batch["skill_id"] if m["speaker"] == "AGENT" else "local",
                            "sequence": m["sequence"], "research_id": m.get("research_id"),
                            "source_refs": m.get("source_refs", [])} for m in messages if not m["deleted"]]
                topic = "\n".join(m["text"] for m in messages if m["id"] in batch["source_message_ids"])
                self._save(batch)
            def charge(result, call_id):
                with self.lock, self.store.transaction():
                    current = self.service.require(rid, "reply_batch")
                    self.store.add("chat_generation", {
                        "reply_batch_id": rid, "conversation_id": current["conversation_id"],
                        "agent_call_id": call_id, "tokens": result.get("tokens"),
                        "provider": result.get("provider"), "cost": result.get("cost"),
                        "discarded": self._cancelled(rid, epoch),
                    }, rid="chatgen_" + digest(call_id)[:32])
                    if current["privacy_revision"] != self.service.privacy_revision:
                        self.service.provider.calls.clear()

            prepared = self.service.agents.respond(
                scope_kind="CHAT", scope_id=batch["conversation_id"], participant_id=batch["skill_id"],
                version_id=batch["skill_version"], personality=self.service.skill_context(skill, topic),
                transcript=public, topic=topic, transport=self.service.provider,
                connection_id=batch.get("connection_id"), model_hash=batch.get("model_hash"),
                cancelled=lambda: self._cancelled(rid, epoch), on_usage=charge,
                web_enabled=self.service.require(batch["conversation_id"], "conversation").get("web_enabled", True))
            with self.lock, self.store.transaction():
                current = self.service.require(rid, "reply_batch")
                if self._cancelled(rid, epoch):
                    return
                current.update(parts=prepared["messages"],
                               idle_followup=None if current.get("idle_cancelled") else prepared.get("idle_followup"),
                               generated=True, generation_id=prepared["generation_id"], agent_id=prepared["agent_id"],
                                research_id=prepared.get("research_id"), source_refs=prepared.get("source_refs", []),
                               status="DELIVERING", next_at=self._at(), failure=None)
                self._save(current)
        except Exception as exc:
            with self.lock, self.store.transaction():
                current = self.store.get(rid, "reply_batch")
                if current and current["epoch"] == epoch and current["status"] == "GENERATING":
                    current.update(status="FAILED", next_at=None,
                                   failure=str(exc) if isinstance(exc, DomainError) else "回复没有完成，请稍后重试")
                    self._user_state(current, "FAILED")
                    self._save(current)
        finally:
            with self.lock:
                self.inflight.discard(rid)

    def _tick(self, *, dispatch=True):
        with self.lock, self.store.transaction():
            batches = self.store.all("reply_batch")
            for batch in batches:
                if batch["status"] not in ACTIVE:
                    continue
                try:
                    self._valid(batch)
                    if batch["status"] == "WAITING_FOLLOWUP" and not self._present(batch["conversation_id"]):
                        self._drop_idle(batch)
                    elif batch["status"] in {"DELIVERING", "WAITING_FOLLOWUP"} and (batch.get("next_at") or "") <= self._at():
                        self._publish(batch)
                except DomainError as exc:
                    self._cancel(batch, str(exc))
            if not dispatch:
                return
            busy_rooms = {self.service.require(rid, "reply_batch")["conversation_id"] for rid in self.inflight}
            candidates = sorted((b for b in self.store.all("reply_batch")
                                 if b["status"] == "QUEUED" and b["id"] not in self.inflight
                                 and b["conversation_id"] not in busy_rooms
                                 and (b.get("next_at") or "") <= self._at()),
                                key=lambda b: (b["next_at"], b["created_at"], b["id"]))
            for batch in candidates[:max(0, 2 - len(self.inflight))]:
                batch.update(status="GENERATING")
                self._save(batch)
                self.inflight.add(batch["id"])
                self.pool.submit(self._generate, batch["id"], batch["epoch"])

    def send_sync(self, conversation_id, text, remember, domain, key):
        receipt = self.accept(conversation_id, text, remember, domain, key, immediate=True)
        rid = receipt["batch"]["id"]
        batch = self.service.require(rid, "reply_batch")
        if batch["status"] in RETRYABLE:
            self.retry(rid)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            self._tick()
            with self.lock:
                batch = self.service.require(rid, "reply_batch")
                if batch["status"] in {"COMPLETED", "WAITING_FOLLOWUP"}:
                    messages = self.service.messages(conversation_id)
                    agents = [m for m in messages if m.get("reply_batch_id") == rid and m["speaker"] == "AGENT"]
                    if not agents:
                        raise DomainError("本次回复未发布", 409)
                    user = next(m for m in messages if m["id"] == receipt["user"]["id"])
                    return {"user": user, "agent": agents[0], "agents": agents, "evidence_id": user["evidence_id"]}
                if batch["status"] in RETRYABLE | {"CANCELLED"}:
                    raise DomainError(batch.get("failure") or "回复未完成，请重试", 502 if batch["status"] in RETRYABLE else 409)
            time.sleep(0.01)
        raise DomainError("回复仍未完成，请稍后查看", 504)

    def invalidate(self, parent, reason):
        with self.lock, self.store.transaction():
            row = self.store.fetchone("SELECT conversation_id FROM messages WHERE evidence_id=?", (parent,))
            affected = row["conversation_id"] if row else None
            for batch in self.store.all("reply_batch"):
                if batch["skill_id"] == parent or batch["conversation_id"] == affected:
                    self._cancel(batch, reason)
                    self._user_state(batch, "REPLIED" if batch["published_count"] else "FAILED")
            if affected:
                conversation = self.service.require(affected, "conversation")
                conversation["redaction_revision"] = conversation.get("reply_revision", 0) + 1
                conversation["reply_revision"] = conversation["redaction_revision"]
                self.store.update(conversation)

    def start_scheduler(self):
        if not self.started and not self.closed:
            self.started = True
            self.thread = threading.Thread(target=self._loop, daemon=True, name="wot-chat-scheduler")
            self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(0.1):
            self._tick()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop_event.set()
        if self.started:
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock, self.store.transaction():
            for batch in self.store.all("reply_batch"):
                if batch["status"] in ACTIVE:
                    if batch["status"] == "WAITING_FOLLOWUP":
                        batch.update(status="COMPLETED", idle_followup=None, next_at=None)
                        self._save(batch)
                        continue
                    batch.update(status="INTERRUPTED", epoch=batch["epoch"] + 1,
                                 idle_followup=None, failure="服务已停止，可手动重试")
                    self._user_state(batch, "FAILED")
                    self._save(batch)
