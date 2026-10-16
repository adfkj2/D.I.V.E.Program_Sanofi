"""Memory and project tool adapter; no model, HTTP or benchmark orchestration."""

import json
from hashlib import sha256

from ..agent.engine import required_text
from .records import ProjectRecord
from .workspace import identity, text


class ProjectAdapter:
    def __init__(self, runtime):
        self.runtime = runtime

    def context(self, project):
        data = self.runtime.project_details(project)
        tasks = data["tasks"]
        completed = sum(bool(task["completed"]) for task in tasks)
        return {
            "name": data["name"],
            "goal": data["goal"],
            # IDs/revisions remain available for validated edits, not narration.
            "tasks": [
                {key: task[key] for key in ("id", "title", "completed", "revision")}
                for task in tasks[:50]
            ],
            "checkpoints": [
                {key: point[key] for key in ("note", "next_step", "created_at")}
                for point in data["checkpoints"][:3]
            ],
            "task_count": len(tasks),
            "task_summary": {"completed": completed, "open": len(tasks) - completed},
            "tasks_truncated": len(tasks) > 50,
            "scope": "仅当前项目的任务状态与最近3条交接；安排和完成状态不代表实验结果",
        }

    def read(self, project, name, arguments):
        if name == "search_memory":
            if set(arguments) != {"query"}:
                raise ValueError("搜索只接受 query。")
            query = required_text(arguments["query"], 4000)
            with self.runtime._lock:
                result = self.runtime.service.retrieve(
                    project, query, limit=8, token_budget=2200
                )
                memories = [item.memory for item in result.items][:8]
                return {
                    "memory_ids": [m.id for m in memories],
                    "records": [
                        {
                            "id": m.id,
                            "content": m.content[:4500],
                            "status": m.status.value,
                            "structured": m.structured_content,
                            "valid_from": m.valid_from,
                            "valid_to": m.valid_to,
                        }
                        for m in memories
                    ],
                }
        if set(arguments) != {"memory_id"}:
            raise ValueError("来源查询只接受 memory_id。")
        memory_id = required_text(arguments["memory_id"], 200)
        data = self.runtime.source(project, memory_id)
        return {
            "memory_ids": [memory_id],
            "memory": data["memory"],
            "events": [
                {
                    "id": event["id"],
                    "observed_at": event["observed_at"],
                    "text": str(event["payload"].get("text", ""))[:6000],
                }
                for event in data["events"][:3]
            ],
        }

    def fingerprint(self, project):
        snapshot = self.runtime.snapshot(project)
        data = {
            "workspace": self.runtime.project_details(project),
            "memories": snapshot["memories"],
        }
        return sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def validate_actions(self, project, actions):
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise ValueError("每次变更须包含 1–8 项操作。")
        details = self.runtime.project_details(project)
        tasks = {task["id"]: task for task in details["tasks"]}
        memories = {m["id"]: m for m in self.runtime.snapshot(project)["memories"]}
        normalized = []
        touched = set()
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("操作须为对象。")
            kind = action.get("kind")
            fields = {
                "add_task": {"title"},
                "update_task": {"task_id", "title", "completed", "revision"},
                "save_record": {
                    "key",
                    "value",
                    "category",
                    "disposition",
                    "reason",
                    "replaces",
                },
                "save_checkpoint": {"note", "next_step"},
            }
            if kind not in fields or set(action) - fields[kind] - {"kind"}:
                raise ValueError("操作名称或字段不受支持。")
            item = dict(action)
            if kind in {"add_task", "update_task"}:
                item["title"] = text(action.get("title", ""), "待办", 500)
            if kind == "update_task":
                old = tasks.get(action.get("task_id"))
                if (
                    old is None
                    or type(action.get("revision")) is not int
                    or old["revision"] != action["revision"]
                ):
                    raise ValueError("待办不存在或版本已变化，请重新读取。")
                if type(action.get("completed")) is not bool:
                    raise ValueError("待办状态必须为布尔值。")
                target = action["task_id"]
            elif kind == "save_record":
                record = ProjectRecord(
                    **{
                        key: action[key]
                        for key in fields[kind] - {"replaces"}
                        if key in action
                    }
                )
                replacement = action.get("replaces")
                old_memory = memories.get(replacement) if replacement else None
                if replacement and (
                    old_memory is None
                    or old_memory["structured_content"].get("key") != record.key
                ):
                    raise ValueError("更新目标须为当前项目同名的有效记录。")
                item.update(
                    key=record.key,
                    value=record.value,
                    category=record.category,
                    disposition=record.disposition,
                    reason=record.reason,
                )
                target = "record:" + record.key.casefold()
            elif kind == "save_checkpoint":
                item["note"] = text(action.get("note", ""), "交接", 4000)
                item["next_step"] = text(
                    action.get("next_step", ""), "下一步", 500, optional=True
                )
                target = "checkpoint"
            else:
                target = "task:" + item["title"]
            if target in touched:
                raise ValueError("同一批次不能重复操作同一目标。")
            touched.add(target)
            normalized.append(item)
        return normalized

    def apply(self, project, run_id, prompt, actions):
        receipts = []
        for index, action in enumerate(actions):
            request_id = f"agent:{run_id}:{index}"
            kind = action["kind"]
            if kind == "add_task":
                self.runtime.add_task(project, action["title"], request_id)
                task_id = identity("task", project, request_id)
                saved = next(
                    t
                    for t in self.runtime.project_details(project)["tasks"]
                    if t["id"] == task_id
                )
                if saved["title"] != action["title"]:
                    raise ValueError("待办写入核验失败，整批操作已回滚。")
                description = "已添加待办：" + action["title"]
            elif kind == "update_task":
                self.runtime.update_task(
                    project,
                    action["task_id"],
                    action["title"],
                    action["completed"],
                    action["revision"],
                )
                saved = next(
                    t
                    for t in self.runtime.project_details(project)["tasks"]
                    if t["id"] == action["task_id"]
                )
                if (
                    saved["title"] != action["title"]
                    or bool(saved["completed"]) != action["completed"]
                ):
                    raise ValueError("待办更新核验失败，整批操作已回滚。")
                description = (
                    "已完成待办：" if action["completed"] else "已更新待办："
                ) + action["title"]
            elif kind == "save_checkpoint":
                self.runtime.checkpoint(
                    project, action["note"], action["next_step"], request_id
                )
                checkpoint_id = identity("checkpoint", project, request_id)
                saved = next(
                    c
                    for c in self.runtime.project_details(project)["checkpoints"]
                    if c["id"] == checkpoint_id
                )
                if (
                    saved["note"] != action["note"]
                    or saved["next_step"] != action["next_step"]
                ):
                    raise ValueError("交接写入核验失败，整批操作已回滚。")
                description = "已保存本次交接" + (
                    "并添加下一步待办" if action["next_step"] else ""
                )
            else:
                record = ProjectRecord(
                    **{
                        k: action[k]
                        for k in ("key", "value", "category", "disposition", "reason")
                    },
                    session=run_id,
                    source_title="用户确认的助手提案",
                    source_excerpt=prompt,
                )
                result = self.runtime.record(
                    project,
                    record,
                    idempotency_key=request_id,
                    replaces=action.get("replaces"),
                )
                matching = [
                    m
                    for m in self.runtime.snapshot(project)["memories"]
                    if m["id"] in result["memory_ids"]
                    and m["structured_content"].get("key") == record.key
                ]
                if not matching or not any(
                    m["structured_content"]["value"] == record.value
                    and m["structured_content"]["disposition"] == record.disposition
                    for m in matching
                ):
                    raise ValueError("记忆写入后核验失败，整批操作已回滚。")
                description = "已保存记忆：" + record.key
            receipts.append(
                {"kind": kind, "description": description, "verified": True}
            )
        return receipts
