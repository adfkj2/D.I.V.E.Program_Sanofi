"""Project adapter and durable runs. Benchmark readers never import this module."""

import json
import os
from threading import Lock

from ..agent.engine import AgentEngine, required_text
from ..agent.protocols import ModelError
from ..agent.transport import CompatibleModel
from ..models import utc_now
from .agent_tools import ProjectAdapter
from .workspace import identity


class Assistant:
    def __init__(self, runtime, model=None):
        self.runtime = runtime
        self.adapter = ProjectAdapter(runtime)
        self.model = model
        self.endpoint = "https://api.deepseek.com"
        self.model_name = "deepseek-chat"
        self._busy = Lock()
        with runtime._lock:
            runtime.service.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS assistant_runs (
                  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, prompt TEXT NOT NULL,
                  payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS assistant_runs_project
                  ON assistant_runs(namespace, created_at);
            """)
            # A stopped process cannot still own a model request.
            rows = runtime.service.store.db.execute(
                "SELECT id,payload FROM assistant_runs"
            ).fetchall()
            with runtime.service.store.transaction():
                for row in rows:
                    payload = json.loads(row["payload"])
                    if payload["status"] == "running":
                        payload.update(
                            status="error",
                            message="上次请求因服务重启中断，请重新发送；没有执行变更。",
                        )
                        runtime.service.store.db.execute(
                            "UPDATE assistant_runs SET payload=? WHERE id=?",
                            (json.dumps(payload), row["id"]),
                        )
        endpoint = os.getenv("DIVE_AGENT_ENDPOINT")
        model_name = os.getenv("DIVE_AGENT_MODEL")
        key = os.getenv("DIVE_AGENT_API_KEY")
        if model is None and any((endpoint, model_name, key)):
            self.configure(
                endpoint or self.endpoint, model_name or self.model_name, key or ""
            )

    def config(self):
        return {
            "configured": self.model is not None,
            "endpoint": self.endpoint,
            "model": self.model_name,
            "max_calls": 6,
            "key_persisted": False,
        }

    def configure(self, endpoint, model, key):
        if not self._busy.acquire(blocking=False):
            raise ValueError("助手正在处理请求，请完成后再更换模型。")
        try:
            candidate = CompatibleModel(endpoint, model, key)
            self.model = candidate
            self.endpoint, self.model_name = candidate.endpoint, candidate.model
            return self.config()
        finally:
            self._busy.release()

    def history(self, project):
        with self.runtime._lock:
            rows = self.runtime.service.store.db.execute(
                "SELECT payload FROM assistant_runs WHERE namespace=? ORDER BY created_at DESC,rowid DESC LIMIT 30",
                (project,),
            ).fetchall()
            return [self.public(json.loads(row["payload"])) for row in reversed(rows)]

    @staticmethod
    def public(payload):
        return {key: value for key, value in payload.items() if key != "fingerprint"}

    def _get(self, project, run_id):
        row = self.runtime.service.store.db.execute(
            "SELECT payload FROM assistant_runs WHERE id=? AND namespace=?",
            (run_id, project),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def _save(self, payload):
        with self.runtime.service.store.transaction():
            self.runtime.service.store.db.execute(
                "INSERT INTO assistant_runs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (
                    payload["id"],
                    payload["project"],
                    payload["prompt"],
                    json.dumps(payload, ensure_ascii=False),
                    payload["created_at"],
                ),
            )

    def chat(self, project, prompt, request_id):
        project = self.runtime._namespace(project)
        prompt = required_text(prompt, 4000)
        run_id = identity("assistant", project, request_id)
        if not self._busy.acquire(blocking=False):
            raise ValueError("助手正在处理请求，请稍后查看对话或重试。")
        try:
            with self.runtime._lock:
                existing = self._get(project, run_id)
                if existing:
                    if existing["prompt"] != prompt:
                        raise ValueError("同一请求编号不能用于不同内容。")
                    return self.public(existing)
                if self.model is None:
                    raise ValueError("请先连接模型，再开始对话；也可以使用项目资料页。")
                previous = self.history(project)
                if any(turn["status"] == "pending" for turn in previous):
                    raise ValueError("请先确认或取消上一份变更提案。")
                payload = {
                    "id": run_id,
                    "project": project,
                    "prompt": prompt,
                    "created_at": utc_now(),
                    "status": "running",
                    "message": "正在读取项目并思考…",
                    "actions": [],
                    "citations": [],
                    "trace": [],
                    "fingerprint": self.adapter.fingerprint(project),
                }
                self._save(payload)
            try:
                result = AgentEngine(self.model, self.adapter).run(
                    project, prompt, previous
                )
                payload.update(result)
            except (ModelError, ValueError, KeyError, TypeError) as error:
                # Do not persist provider payloads, exceptions, headers or keys.
                message = (
                    str(error)
                    if isinstance(error, ModelError)
                    else "助手响应未通过校验，请重新描述请求后重试。"
                )
                payload.update(status="error", message=message)
            with self.runtime._lock:
                if payload["status"] != "error" and payload[
                    "fingerprint"
                ] != self.adapter.fingerprint(project):
                    payload.update(
                        status="error",
                        actions=[],
                        citations=[],
                        message="处理期间项目已变化，请重新发送以使用最新资料。",
                    )
                self._save(payload)
            return self.public(payload)
        finally:
            self._busy.release()

    def decide(self, project, run_id, approve):
        with self.runtime._lock, self.runtime.service.store.transaction():
            payload = self._get(project, run_id)
            if payload is None:
                raise KeyError("proposal not found")
            if payload["status"] in {"applied", "cancelled"}:
                return self.public(payload)
            if payload["status"] != "pending":
                raise ValueError("这轮对话没有待确认的变更。")
            if not approve:
                payload.update(
                    status="cancelled", message="已取消提案，项目资料未修改。"
                )
            else:
                if payload["fingerprint"] != self.adapter.fingerprint(project):
                    raise ValueError(
                        "项目资料已变化，请取消旧提案，再让助手按最新状态生成。"
                    )
                actions = self.adapter.validate_actions(project, payload["actions"])
                receipts = self.adapter.apply(
                    project, run_id, payload["prompt"], actions
                )
                payload.update(
                    status="applied",
                    receipts=receipts,
                    message="已执行并核验：\n"
                    + "\n".join(r["description"] for r in receipts),
                )
                payload["trace"].append(
                    {"step": "verify", "label": "操作已写入并逐项读取核验"}
                )
            self._save(payload)
            return self.public(payload)
