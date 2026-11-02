"""Service-owned fair slow-chat scheduler; browser presence is irrelevant."""

import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .agents import AgentLimit
from .catalog import role_context
from .domain import DomainError, ResearchChanged
from .storage import digest, now, uid


def after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class VirtualSpace:
    def __init__(
        self,
        service,
        people,
        connections,
        assessments,
        *,
        interval=(12, 25),
        intervention_seconds=5,
    ):
        self.service, self.store = service, service.store
        self.people, self.connections, self.assessments = people, connections, assessments
        self.interval = interval
        self.intervention_seconds = max(5, intervention_seconds)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wot-virtual")
        self.inflight = set()
        self.started = False
        with self.store.transaction():
            for session in self.store.all("virtual_session"):
                # A legacy turn was a single bubble. New bursts keep speaker
                # ownership independently from the number of published bubbles.
                migrated = "next_speaker_index" not in session
                session.setdefault("next_speaker_index", session["published_count"] % 2)
                session.setdefault("burst_pending", [])
                session.setdefault("burst_published_count", 0)
                session.setdefault("pause_reason", None)
                if "max_web_requests" not in session:
                    session.update(max_web_requests=20, used_web_requests=0, web_enabled=True)
                    migrated = True
                if "agent_ids" not in session:
                    session["agent_ids"] = {}
                    migrated = True
                for participant in session["participants"]:
                    session["agent_ids"][participant["id"]] = self.service.agents.ensure(
                        scope_kind="VIRTUAL", scope_id=session["id"],
                        participant_id=participant["id"], version_id=participant["version_id"],
                    )
                if session["state"] in {"RUNNING", "GENERATING", "INTERVENING"}:
                    self._discard_burst(session, handoff=True)
                    self.service.agents.pause_scope(session["id"], "服务重新启动，会话已暂停", clear_pending=True)
                    session.update(
                        state="PAUSED",
                        typing=False,
                        draft=None,
                        failure="服务重新启动，会话已暂停；请手动继续",
                        epoch=session.get("epoch", 0) + 1,
                    )
                    self.store.update(session)
                elif migrated:
                    self.store.update(session)

    def start_scheduler(self):
        if not self.started:
            self.started = True
            self.thread = threading.Thread(target=self._loop, daemon=True, name="wot-scheduler")
            self.thread.start()

    def participants(self, connection_id=None):
        result = []
        for role in self.store.all("skill"):
            if role["status"] == "ACTIVE":
                version = digest({"text": role.get("text", ""), "version": role.get("version", 1)})
                result.append(
                    {
                        "id": role["id"],
                        "version_id": version,
                        "kind": "CHARACTER",
                        "display_name": role["display_name"],
                        "avatar_url": role.get("avatar_url", ""),
                    }
                )
        admission = self.assessments.self_admission()
        if admission["eligible"]:
            version = admission["version_id"]
            result.insert(
                0,
                {
                    "id": "self:" + version,
                    "version_id": version,
                    "kind": "SELF",
                    "display_name": self.people.profile()["nickname"],
                    "avatar_url": self.people.profile()["avatar_url"],
                },
            )
        return result

    def create(self, data):
        if set(data) - {
            "left_id",
            "right_id",
            "topic",
            "sharing_approved",
            "max_turns",
            "max_tokens",
            "start_immediately",
            "web_enabled",
            "max_web_requests",
        }:
            raise DomainError("会话配置包含未知字段")
        participants = {p["id"]: p for p in self.participants()}
        def participant(rid):
            candidate = participants.get(rid)
            if candidate or not isinstance(rid, str) or not rid.startswith("self:"):
                return candidate
            # Older passed versions remain usable even when a newer draft is
            # accumulating. Validate the exact version, never the dropdown.
            version = rid.removeprefix("self:")
            self.assessments.assert_self_admitted(version)
            return {"id": rid, "version_id": version, "kind": "SELF",
                    "display_name": self.people.profile()["nickname"],
                    "avatar_url": self.people.profile()["avatar_url"]}

        left, right = participant(data.get("left_id")), participant(data.get("right_id"))
        if not left or not right:
            raise DomainError("请选择已准入的人物或已校验分身")
        if left["version_id"] == right["version_id"]:
            raise DomainError("两侧不能选择同一人物版本")
        topic = data.get("topic", "")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 2000:
            raise DomainError("请填写初始话题，最多 2000 字")
        if data.get("sharing_approved") is not True:
            raise DomainError("开始前请授权双方自动分享本场 AI 生成发言")
        max_turns = data.get("max_turns", 20)
        max_tokens = data.get("max_tokens", 30000)
        start_immediately = data.get("start_immediately", False)
        web_enabled = data.get("web_enabled", True)
        max_web_requests = data.get("max_web_requests", 20)
        if (
            type(max_turns) is not int
            or not 2 <= max_turns <= 20
            or type(max_tokens) is not int
            or not 1000 <= max_tokens <= 30000
            or type(start_immediately) is not bool
            or type(web_enabled) is not bool
            or type(max_web_requests) is not int
            or not 1 <= max_web_requests <= 20
        ):
            raise DomainError("会话限额无效")
        connection = self.connections.active()
        if not connection or not self.connections.is_live(connection["id"]):
            raise DomainError("虚拟空间需要配置可用的真实模型连接", 503)
        with self.store.transaction():
            session = self.store.add(
                "virtual_session",
                {
                    "topic": topic.strip(),
                    "participants": [left, right],
                    "state": "CREATED",
                    "connection_id": connection["id"],
                    "model_hash": self.connections.config_hash(),
                    "model": connection["model"],
                    "max_turns": max_turns,
                    "max_tokens": max_tokens,
                    "used_tokens": 0,
                    "web_enabled": web_enabled,
                    "max_web_requests": max_web_requests,
                    "used_web_requests": 0,
                    "draft": None,
                    "typing": False,
                    "revision": 1,
                    "epoch": 0,
                    "next_at": None,
                    "last_dispatched": "",
                    "sharing_approved_at": now(),
                    "cost": None,
                    "cost_label": "费用未估算",
                    "failure": None,
                    "context_corrections": [],
                    "published_count": 0,
                    "next_speaker_index": 0,
                    "burst_pending": [],
                    "burst_published_count": 0,
                    "pause_reason": None,
                    "agent_ids": {},
                },
            )
            for participant in session["participants"]:
                session["agent_ids"][participant["id"]] = self.service.agents.ensure(
                    scope_kind="VIRTUAL", scope_id=session["id"],
                    participant_id=participant["id"], version_id=participant["version_id"],
                )
            self.store.update(session)
            self.store.audit("VIRTUAL_SESSION_CONSENT", session["id"])
        return self.control(session["id"], "start") if start_immediately else self.view(session["id"])

    def _session(self, rid):
        return self.service.require(rid, "virtual_session")

    def _validate(self, s):
        if not self.connections.is_live(s["connection_id"]):
            raise DomainError("本场模型连接不可用，请检查模型配置", 503)
        if self.connections.config_hash(s["connection_id"]) != s["model_hash"]:
            raise DomainError("本场模型配置已改变，请重新选择模型后新建会话", 409)
        for p in s["participants"]:
            if p["kind"] == "SELF":
                if p["id"] != "self:" + p["version_id"]:
                    raise DomainError("本人分身版本不匹配", 403)
                self.assessments.assert_self_admitted(p["version_id"])
                continue
            actual = self.store.get(p["id"], "skill")
            version = digest({"text": actual.get("text", ""), "version": actual.get("version", 1)}) if actual else None
            if not actual or actual["status"] != "ACTIVE" or version != p["version_id"]:
                raise DomainError("参与者已停用或版本已改变", 409)
        for correction in s.get("context_corrections", []):
            source = self.store.fetchone(
                "SELECT status FROM evidence WHERE id=? AND owner_id='local'",
                (correction["evidence_id"],),
            )
            if not source or source["status"] != "ACTIVE":
                raise DomainError("本场更正的来源已撤回，请新建会话", 409)
        for event in self.store.all("intervention"):
            if event["session_id"] == s["id"] and event["scope"] == "IDEAL_DIRECTION":
                directive = self.store.get(event.get("source_id"), "directive")
                if not directive or directive["state"] != "APPROVED":
                    raise DomainError("本场理想方向已撤回，请新建会话", 409)

    def _save(self, s):
        s["revision"] += 1
        self.store.update(s)

    def view(self, rid):
        # A turn and its quota/state transition are committed together. Keep
        # that same snapshot while reading both; separate reads can otherwise
        # combine an older RUNNING session with newly published final turns.
        # Store lock only: readers must not reverse service/virtual lock order.
        with self.store.lock:
            return self._view(rid)

    def _view(self, rid):
        s = self._session(rid)
        # Never accidentally publish a newly added internal field. In
        # particular the tail of a burst is not shared dialogue or a preview.
        fields = {
            "id", "created_at", "topic", "state", "connection_id", "model_hash", "model",
            "max_turns", "max_tokens", "used_tokens", "draft", "typing", "revision", "next_at",
            "sharing_approved_at", "cost", "cost_label", "failure", "published_count",
            "pause_reason",
            "web_enabled", "max_web_requests", "used_web_requests",
        }
        public = {
            k: v
            for k, v in s.items()
            if k in fields
        }
        draft = s.get("draft")
        if draft is not None and (not isinstance(draft, dict) or not any(
            p["id"] == draft.get("speaker_id") and p["kind"] == "SELF" for p in s["participants"]
        )):
            # Character text is not yet a published message. Only the human's
            # own avatar draft needs a public preview and takeover controls.
            public["draft"] = None
        # Display cosmetics always reflect current choices; turn source versions
        # remain immutable and are never inferred from the current selection.
        participants = []
        for p in s["participants"]:
            role = (
                self.people.profile()
                if p["kind"] == "SELF"
                else self.store.get(p["id"], "skill") or {}
            )
            participants.append(
                {
                    **p,
                    "avatar_url": role.get("avatar_url", p.get("avatar_url", "")),
                    "display_name": role.get(
                        "nickname", role.get("display_name", p["display_name"])
                    ),
                }
            )
        public["participants"] = participants
        research = getattr(self.service, "research", None)
        progress = research.progress(rid) if research else None
        public.update(research_phase=progress.get("phase") if progress and s["state"] == "GENERATING" else None,
                      research_status=progress.get("status") if progress else None,
                      research_notice=progress.get("notice") if progress else None)
        if public.get("typing"):
            speaker = participants[s.get("next_speaker_index", s["published_count"] % 2)]
            public["typing_speaker_id"] = speaker["id"]
            public["typing_display_name"] = speaker["display_name"]
        public["turns"] = [t for t in self.store.all("virtual_turn") if t["session_id"] == rid]
        for turn in public["turns"]:
            p = next(p for p in participants if p["id"] == turn["speaker_id"])
            turn.update(avatar_url=p.get("avatar_url", ""), display_name=p["display_name"])
        fields = {
            "id",
            "draft_id",
            "scope",
            "source_id",
            "participant_version",
            "human_confirmed",
            "created_at",
            "text",
        }
        public["interventions"] = [
            {key: value for key, value in event.items() if key in fields}
            for event in self.store.all("intervention")
            if event["session_id"] == rid
        ]
        return public

    def list(self):
        return [self.view(s["id"]) for s in reversed(self.store.all("virtual_session"))]

    def control(self, rid, action):
        with self.lock, self.store.transaction():
            s = self._session(rid)
            if action in {"start", "resume"}:
                self._validate(s)
                if s["state"] not in {"CREATED", "PAUSED", "PAUSED_LIMIT", "INCOMPLETE"}:
                    raise DomainError("此会话当前不能启动", 409)
                if s["used_tokens"] >= s["max_tokens"] or s["published_count"] >= s["max_turns"]:
                    raise DomainError("会话已达到限额", 409)
                if (s.get("pause_reason") == "WEB_REQUEST_LIMIT" and s.get("web_enabled", True)
                        and s.get("used_web_requests", 0) >= s.get("max_web_requests", 20)):
                    raise DomainError("联网请求额度已用完，请追加额度或关闭本场联网后继续", 409)
                s.update(
                    state="RUNNING",
                    failure=None,
                    pause_reason=None,
                    next_at=after(0 if not s["published_count"] else self.interval[0]),
                )
                if s.get("draft"):
                    # Limit pauses preserve a paid outbox. A SELF preview must
                    # again get a full intervention window on manual resume.
                    self._set_next_draft(s, s["draft"], first=True)
                elif s.get("burst_pending"):
                    self._set_next_draft(s, s["burst_pending"].pop(0), first=True)
            elif action == "pause":
                if s["state"] not in {"COMPLETED", "STOPPED", "INVALIDATED"}:
                    self._discard_burst(s, handoff=True)
                    self.service.agents.pause_scope(rid, clear_pending=True)
                    s.update(state="PAUSED", typing=False, draft=None, epoch=s["epoch"] + 1)
            elif action == "stop":
                self._discard_burst(s)
                self.service.agents.pause_scope(rid, "会话已停止", clear_pending=True)
                s.update(state="STOPPED", typing=False, draft=None, epoch=s["epoch"] + 1)
            else:
                raise DomainError("未知会话操作", 404)
            self._save(s)
        return self.view(rid)

    def top_up(self, rid, data):
        """The router supplies the durable idempotency boundary; never resume."""
        if set(data) - {"add_messages", "add_tokens", "add_web_requests"}:
            raise DomainError("额度配置包含未知字段")
        messages, tokens = data.get("add_messages", 0), data.get("add_tokens", 0)
        web_requests = data.get("add_web_requests", 0)
        if (type(messages) is not int or not 0 <= messages <= 20
                or type(tokens) is not int or not 0 <= tokens <= 30_000
                or type(web_requests) is not int or not 0 <= web_requests <= 20
                or messages + tokens + web_requests == 0):
            raise DomainError("每次可追加0至20条消息、0至30,000 token及0至20次联网请求，至少选择一项")
        with self.lock, self.store.transaction():
            s = self._session(rid)
            if s["state"] not in {"CREATED", "PAUSED", "PAUSED_LIMIT", "INCOMPLETE"}:
                raise DomainError("请先暂停会话再追加额度", 409)
            self._validate(s)
            s["max_turns"] += messages
            s["max_tokens"] += tokens
            s["max_web_requests"] = s.get("max_web_requests", 20) + web_requests
            self._save(s)
            self.store.audit("VIRTUAL_BUDGET_ADDED", rid,
                             {"add_messages": messages, "add_tokens": tokens, "add_web_requests": web_requests})
        return self.view(rid)

    def _pause_limit(self, s, reason):
        messages = {
            "MESSAGE_LIMIT": "本场消息额度已用完，可追加额度后继续",
            "TOKEN_LIMIT": "本场token额度已用完，可追加额度后继续",
            "BUDGET_PRECHECK": "剩余额度不足以完整生成下一轮，可追加额度后继续",
            "WEB_REQUEST_LIMIT": "本场联网请求额度已用完，可追加额度后继续",
        }
        s.update(state="PAUSED_LIMIT", typing=False, pause_reason=reason,
                 failure=messages.get(reason, messages["BUDGET_PRECHECK"]), next_at=None)
        self.service.agents.pause_scope(s["id"], s["failure"])
        self._save(s)

    def web(self, rid, enabled):
        if type(enabled) is not bool:
            raise DomainError("联网开关无效")
        with self.lock, self.store.transaction():
            s = self._session(rid)
            if s.get("web_enabled", True) != enabled:
                self._discard_burst(s, handoff=True)
                self.service.agents.pause_scope(rid, "联网设置已改变", clear_pending=True)
                s.update(web_enabled=enabled, epoch=s["epoch"] + 1, typing=False,
                         state="PAUSED" if s["state"] in {"RUNNING", "GENERATING", "INTERVENING"} else s["state"],
                         failure="联网设置已改变，点击继续后生效")
                research = getattr(self.service, "research", None)
                if research:
                    research.invalidate_scope(rid, "联网设置已改变", pending_only=True)
                self._save(s)
        return self.view(rid)

    def pause_all(self):
        for s in self.store.all("virtual_session"):
            if s["state"] in {"RUNNING", "GENERATING", "INTERVENING"}:
                self.control(s["id"], "pause")
        return self.list()

    def takeover(self, rid):
        with self.lock, self.store.transaction():
            s = self._session(rid)
            self._validate(s)
            if not s.get("draft") or s["state"] != "RUNNING":
                raise DomainError("当前没有可接管的分身发言", 409)
            p = next(p for p in s["participants"] if p["id"] == s["draft"]["speaker_id"])
            if p["kind"] != "SELF":
                raise DomainError("只能接管自己的分身", 403)
            # A correction changes the premise of every not-yet-spoken part.
            # Keep only the currently visible draft for the human to rewrite.
            s.update(state="INTERVENING", typing=False, burst_pending=[])
            self.service.agents.pause_scope(rid, "本人正在接管这一句", clear_pending=True)
            self._save(s)
        return self.view(rid)

    def intervene(self, rid, data):
        if set(data) - {"text", "scope", "confirmed"}:
            raise DomainError("干预包含未知字段")
        text, scope = data.get("text", ""), data.get("scope")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 4000
            or scope not in {"TURN_ONLY", "REAL_VIEW", "IDEAL_DIRECTION"}
        ):
            raise DomainError("请选择干预用途并填写发言")
        token = uid("intervention_pending")
        with self.lock, self.store.transaction():
            s = self._session(rid)
            self._validate(s)
            if s["state"] != "INTERVENING" or not s.get("draft"):
                raise DomainError("请先接管当前发言", 409)
            if scope != "TURN_ONLY" and data.get("confirmed") is not True:
                raise DomainError("请明确确认这条真实观点或理想方向")
            if s.get("intervention_pending"):
                raise DomainError("本次干预正在保存，请稍候", 409)
            epoch, draft_id = s["epoch"], s["draft"]["id"]
            s["intervention_pending"] = token
            self._save(s)
        # Human memory writes may acquire service.lock. Never hold virtual.lock
        # while doing that: evidence deletion enters this subsystem in reverse.
        source = None
        try:
            if scope == "REAL_VIEW":
                source = self.people.statement(text, "USER_CORRECTION")
                self.people.user_skill()  # accumulate a new draft; no grant promotion
            elif scope == "IDEAL_DIRECTION":
                source = self.service.propose(text, "general", "虚拟空间中由本人明确选择的理想方向")
                source = self.service.directive(source["id"], source["revision"], "approve")
                self.people.user_skill()  # chosen remains an aspiration in the next draft
            with self.lock, self.store.transaction():
                s = self._session(rid)
                if (
                    s["epoch"] != epoch
                    or s["state"] != "INTERVENING"
                    or not s.get("draft")
                    or s["draft"]["id"] != draft_id
                    or s.get("intervention_pending") != token
                ):
                    raise DomainError("会话状态已改变；已确认的本人资料仍已保存，请刷新后重试", 409)
                self._validate(s)
                if scope == "REAL_VIEW":
                    evidence = self.service.evidence(source["id"])
                    if evidence["status"] != "ACTIVE":
                        raise DomainError("这条更正的来源已撤回，未重新发布", 409)
                    s["context_corrections"].append({"text": text, "evidence_id": source["id"]})
                elif (
                    scope == "IDEAL_DIRECTION"
                    and self.service.require(source["id"], "directive")["state"] != "APPROVED"
                ):
                    raise DomainError("该理想方向已撤回，未重新发布", 409)
                event = self.store.add(
                    "intervention",
                    {
                        "session_id": rid,
                        "draft_id": s["draft"]["id"],
                        "scope": scope,
                        "text": text,
                        "source_id": source["id"] if source else None,
                        "participant_version": s["draft"]["version_id"],
                        "human_confirmed": data.get("confirmed", False),
                    },
                )
                s["draft"].update(
                    text=text,
                    intervention_id=event["id"],
                    research_id=None,
                    source_refs=[],
                    publish_at=after(self.intervention_seconds),
                )
                s.pop("intervention_pending", None)
                s.update(state="RUNNING")
                self._save(s)
        except Exception:
            with self.lock, self.store.transaction():
                current = self._session(rid)
                if current.get("intervention_pending") == token:
                    current.pop("intervention_pending", None)
                    self._save(current)
            raise
        return self.view(rid)

    def _discard_burst(self, s, *, handoff=False):
        if handoff and s.get("burst_published_count", 0):
            s["next_speaker_index"] = 1 - s.get("next_speaker_index", 0)
        s.update(draft=None, burst_pending=[], burst_published_count=0)
        s.pop("burst_generation_id", None)

    def _part_delay(self, text):
        return min(4, max(1, 1 + len(text) / 80 + random.random() * 0.7))

    def _set_next_draft(self, s, draft, *, first=False):
        participant = next(p for p in s["participants"] if p["id"] == draft["speaker_id"])
        delay = self.intervention_seconds if participant["kind"] == "SELF" else (
            0 if first else self._part_delay(draft["text"])
        )
        s.update(
            draft={**draft, "publish_at": after(delay)},
            typing=participant["kind"] != "SELF",
        )

    def _publish(self, s):
        self._validate(s)
        d = s["draft"]
        research = getattr(self.service, "research", None)
        if research and d.get("research_id") and research.stale(d["research_id"]):
            raise ResearchChanged()
        if s["published_count"] >= s["max_turns"]:
            self._pause_limit(s, "MESSAGE_LIMIT")
            return
        turn = self.store.add(
            "virtual_turn",
            {
                **{k: v for k, v in d.items() if k not in {"id", "publish_at"}},
                "session_id": s["id"],
                "turn_index": s["published_count"],
                "is_generated": True,
                "disclosure": "AI 模拟",
                "published_at": now(),
            },
            rid=d["id"],
        )
        if research and d.get("research_id") and not d.get("intervention_id"):
            turn.update(research.publish(d["research_id"], d["id"], d.get("part_index", 0), d["text"]))
            self.store.update(turn)
        self.service.agents.acknowledge(d.get("agent_id"), d.get("part_index"),
                                        d.get("generation_id"))
        s["published_count"] += 1
        s["burst_published_count"] = s.get("burst_published_count", 0) + 1
        pending = s.get("burst_pending", [])
        if pending:
            self._set_next_draft(s, pending.pop(0))
            s["burst_pending"] = pending
            if s["published_count"] >= s["max_turns"]:
                self._pause_limit(s, "MESSAGE_LIMIT")
            else:
                self._save(s)
            return
        # Longer utterances get a longer reading gap, with bounded variation.
        delay = min(
            self.interval[1],
            max(self.interval[0], self.interval[0] + len(d["text"]) / 28 + random.random() * 3),
        )
        self._discard_burst(s, handoff=True)
        s.update(typing=False, next_at=after(delay))
        # Already paid, in-budget bursts may finish at the token limit. Only
        # after their last bubble do we close the gate to another generation.
        if s["published_count"] >= s["max_turns"]:
            self._pause_limit(s, "MESSAGE_LIMIT")
        elif s["used_tokens"] >= s["max_tokens"]:
            self._pause_limit(s, "TOKEN_LIMIT")
        else:
            self._save(s)

    def _tick(self):
        with self.lock, self.store.transaction():
            sessions = self.store.all("virtual_session")
            for s in sessions:
                if s["state"] == "RUNNING" and s.get("draft") and s["draft"]["publish_at"] <= now():
                    try:
                        self._publish(s)
                    except DomainError as exc:
                        self._discard_burst(s)
                        s.update(state="PAUSED" if isinstance(exc, ResearchChanged) else "INVALIDATED",
                                 draft=None, typing=False, failure=str(exc), epoch=s["epoch"] + 1)
                        self.service.agents.pause_scope(s["id"], str(exc), clear_pending=True)
                        self._save(s)
            available = max(0, 2 - len(self.inflight))
            candidates = sorted(
                (
                    s
                    for s in self.store.all("virtual_session")
                    if s["state"] == "RUNNING"
                    and not s.get("draft")
                    and s["id"] not in self.inflight
                    and (s.get("next_at") or "") <= now()
                ),
                key=lambda s: (s["last_dispatched"], s["created_at"], s["id"]),
            )
            for s in candidates[:available]:
                self.inflight.add(s["id"])
                s.update(state="GENERATING", typing=True, last_dispatched=now())
                self._save(s)
                self.pool.submit(self._generate, s["id"], s["epoch"])

    def _reserve_web_request(self, rid, epoch, request_id):
        """Reserve once, refund only a request cancelled before HTTP dispatch.

        The returned settler is process-local; it never exposes credentials or
        changes the durable idempotency key. A failed/uncertain network attempt
        remains dispatched and is never refunded as though it were free.
        """
        reservation_id = "vweb_" + digest([rid, request_id])[:32]
        with self.lock, self.store.transaction():
            current = self._session(rid)
            if self._cancelled(rid, epoch):
                raise DomainError("本次查询已取消", 409)
            record = self.store.get(reservation_id, "virtual_web_reservation")
            if record and record.get("state") == "NOT_SENT":
                raise DomainError("这次查询的预留已取消", 409)
            if record is None:
                if current.get("used_web_requests", 0) >= current.get("max_web_requests", 20):
                    raise AgentLimit("WEB_REQUEST_LIMIT")
                current["used_web_requests"] = current.get("used_web_requests", 0) + 1
                self.store.add("virtual_web_reservation", {
                    "session_id": rid, "request_id": request_id, "epoch": epoch, "state": "RESERVED",
                }, rid=reservation_id)
                self._save(current)

        def settle(sent):
            with self.lock, self.store.transaction():
                reservation = self.store.get(reservation_id, "virtual_web_reservation")
                if not reservation or reservation.get("state") != "RESERVED":
                    return
                reservation["state"] = "DISPATCHED" if sent else "NOT_SENT"
                self.store.update(reservation)
                if not sent:
                    latest = self._session(rid)
                    latest["used_web_requests"] = max(0, latest.get("used_web_requests", 0) - 1)
                    self._save(latest)

        return settle

    def _generate(self, rid, epoch):
        try:
            s = self._session(rid)
            self._validate(s)
            p = s["participants"][s.get("next_speaker_index", s["published_count"] % 2)]
            remaining_messages = min(4, s["max_turns"] - s["published_count"])
            if remaining_messages <= 0:
                with self.lock, self.store.transaction():
                    current = self._session(rid)
                    if current["epoch"] == epoch:
                        self._pause_limit(current, "MESSAGE_LIMIT")
                return
            published = self.view(rid)["turns"]
            if p["kind"] == "SELF":
                personality = self.people.user_skill(p["version_id"])["text"]
            else:
                personality = role_context(
                    self.service.require(p["id"], "skill"),
                    s["topic"] + "\n" + "\n".join(t["text"] for t in published[-2:]),
                )
            transcript = [
                {"id": t["id"], "speaker": t["display_name"],
                 "speaker_id": t["speaker_id"], "text": t["text"],
                  "sequence": t["turn_index"], "research_id": t.get("research_id"),
                  "source_refs": t.get("source_refs", [])}
                for t in published
            ]

            def charge(result, call_id):
                # Every actual attempt is charged once, before cancellation,
                # parser errors or an empty visible reasoning-model response.
                with self.lock, self.store.transaction():
                    current = self._session(rid)
                    record_id = "vgen_" + digest(call_id)[:32]
                    if self.store.get(record_id, "virtual_generation"):
                        return
                    usage = result["tokens"]
                    current["used_tokens"] += usage
                    self.store.add("virtual_generation", {
                        "session_id": rid, "speaker_id": p["id"],
                        "version_id": p["version_id"], "tokens": usage,
                        "model_hash": s["model_hash"], "epoch": epoch,
                        "agent_call_id": call_id,
                    }, rid=record_id)
                    self._save(current)
                if result.get("classification") != "LIVE":
                    raise DomainError("虚拟空间需要真实模型连接", 409)

            result = self.service.agents.respond(
                scope_kind="VIRTUAL", scope_id=rid, participant_id=p["id"],
                version_id=p["version_id"], personality=personality,
                transcript=transcript, topic=s["topic"], transport=self.connections,
                connection_id=s["connection_id"], model_hash=s["model_hash"],
                corrections=s.get("context_corrections", []),
                cancelled=lambda: self._cancelled(rid, epoch),
                allow_followup=False, max_messages=remaining_messages,
                on_usage=charge,
                remaining_tokens=lambda: self._session(rid)["max_tokens"] - self._session(rid)["used_tokens"],
                web_enabled=s.get("web_enabled", True),
                on_web_request=lambda request_id: self._reserve_web_request(rid, epoch, request_id),
            )
            with self.lock, self.store.transaction():
                current = self._session(rid)
                if current["epoch"] != epoch or current["state"] != "GENERATING":
                    return
                self._validate(current)
                parts = [
                    {"id": uid("vturn"), "speaker_id": p["id"],
                     "display_name": p["display_name"], "avatar_url": p.get("avatar_url", ""),
                     "version_id": p["version_id"], "text": text,
                     "model_hash": s["model_hash"], "connection_id": s["connection_id"],
                     "classification": "AI_SIMULATED", "agent_id": result["agent_id"],
                      "generation_id": result["generation_id"], "part_index": index,
                      "research_id": result.get("research_id"), "source_refs": []}
                    for index, text in enumerate(result["messages"])
                ]
                current.setdefault("agent_ids", {})[p["id"]] = result["agent_id"]
                current.update(state="RUNNING", burst_generation_id=result["generation_id"],
                               burst_published_count=0, burst_pending=parts[1:])
                self._set_next_draft(current, parts[0], first=True)
                if current["used_tokens"] > current["max_tokens"]:
                    self._pause_limit(current, "TOKEN_LIMIT")
                else:
                    self._save(current)
        except Exception as exc:
            with self.lock, self.store.transaction():
                s = self._session(rid)
                if s["epoch"] == epoch and s["state"] == "GENERATING":
                    if isinstance(exc, AgentLimit):
                        self._pause_limit(s, exc.reason)
                    else:
                        self._discard_burst(s)
                        failure = exc
                        try:
                            self._validate(s)
                        except DomainError as invalid:
                            failure = invalid
                        s.update(state="INCOMPLETE", typing=False, draft=None,
                                 failure=str(failure) if isinstance(failure, DomainError)
                                 else "本轮发言未完成，请检查连接后手动继续")
                        self._save(s)
        finally:
            with self.lock:
                self.inflight.discard(rid)

    def _cancelled(self, rid, epoch):
        if self.stop_event.is_set():
            return True
        current = self._session(rid)
        if current["epoch"] != epoch or current["state"] != "GENERATING":
            return True
        try:
            self._validate(current)
        except DomainError:
            return True
        return False

    def _loop(self):
        while not self.stop_event.wait(0.2):
            try:
                self._tick()
            except Exception:
                # No private content or model exceptions are logged. A persisted
                # session is retried only for scheduling, never transport calls.
                self.stop_event.wait(0.5)

    def invalidate(self, parent, reason):
        # The caller may already hold service.lock and store.lock. Store-level
        # serialization alone is enough to atomically invalidate epochs/drafts;
        # taking virtual.lock here would invert every scheduler mutation lock.
        with self.store.transaction():
            for s in self.store.all("virtual_session"):
                source_affected = any(
                    p["id"] == parent
                    or p["version_id"] == parent
                    or parent
                    in [*version.get("evidence_ids", []), *version.get("directive_ids", [])]
                    for p in s["participants"]
                    for version in [self.store.get(p["version_id"], "user_skill") or {}]
                )
                source_affected |= any(
                    c["evidence_id"] == parent for c in s.get("context_corrections", [])
                )
                source_affected |= any(
                    e.get("source_id") == parent
                    for e in self.store.all("intervention")
                    if e["session_id"] == s["id"]
                )
                gate_locked = any(
                    p["kind"] == "SELF"
                    and not self.assessments.self_admission(p["version_id"])["eligible"]
                    for p in s["participants"]
                )
                active = s["state"] not in {"COMPLETED", "STOPPED", "INVALIDATED"}
                if source_affected or (gate_locked and active):
                    self._discard_burst(s)
                    s.update(
                        state="INVALIDATED",
                        epoch=s["epoch"] + 1,
                        typing=False,
                        draft=None,
                        context_corrections=[],
                        failure=reason,
                    )
                    s.pop("intervention_pending", None)
                    self._save(s)
                    # Actor summaries/checkpoints are private derivatives too.
                    # Clear them inside this same deletion serialization boundary.
                    for actor in self.store.all("agent_instance"):
                        if actor["scope_kind"] == "VIRTUAL" and actor["scope_id"] == s["id"]:
                            actor.update(state="INVALIDATED", epoch=actor.get("epoch", 0) + 1,
                                         summary=[], pending_messages=[], checkpoint=None,
                                         failure=reason)
                            self.store.update(actor)
                    # Removing a character preserves historical published chat.
                    # Revoked personal evidence must also scrub derived text.
                    # Personal admission changes do not affect unrelated
                    # character sessions. Only actual dependencies are scrubbed.
                    if source_affected and not self.store.get(parent, "skill"):
                        for t in self.store.all("virtual_turn"):
                            if t["session_id"] == s["id"]:
                                t["text"] = "[依赖来源已撤回]"
                                self.store.update(t)
                        for e in self.store.all("intervention"):
                            if e["session_id"] == s["id"]:
                                e["text"] = "[依赖来源已撤回]"
                                self.store.update(e)

    def close(self):
        self.stop_event.set()
        if self.started:
            self.thread.join(timeout=3)
        self.pause_all()
        self.pool.shutdown(wait=True, cancel_futures=True)
