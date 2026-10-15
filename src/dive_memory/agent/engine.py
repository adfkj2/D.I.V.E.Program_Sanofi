"""Read → reason/tool → propose/respond. All mutations live behind confirmation."""

import json

from .protocols import ChatModel, ModelError, ProjectTools
from .schema import SYSTEM, TOOLS


def required_text(value, limit=6000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("模型返回了空内容或超长内容。")
    return value.strip()


class AgentEngine:
    def __init__(self, model: ChatModel, tools: ProjectTools):
        self.model, self.tools = model, tools

    def run(self, project: str, prompt: str, history: list[dict]) -> dict:
        context = self.tools.context(project)
        messages: list[dict] = [{"role": "system", "content": SYSTEM}]
        for turn in history[-6:]:
            if turn["status"] in {"completed", "applied"}:
                messages.extend(
                    [
                        {"role": "user", "content": turn["prompt"][:4000]},
                        {"role": "assistant", "content": turn["message"][:6000]},
                    ]
                )
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_project_data": context,
                        "request": prompt,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        seen = set()
        trace = [{"step": "context", "label": "已读取项目目标、待办与上次交接"}]
        for _ in range(6):
            response = self.model.complete(messages, TOOLS)
            calls = response.get("tool_calls")
            if not isinstance(calls, list) or len(calls) != 1:
                raise ModelError("模型没有返回单个有效工具操作，请重试。")
            call = calls[0]
            try:
                call_id = required_text(call["id"], 200)
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数须为对象。")
                if name == "respond":
                    if set(arguments) != {"message", "citations"}:
                        raise ValueError("回答字段无效。")
                    citations = arguments["citations"]
                    if (
                        not isinstance(citations, list)
                        or len(citations) > 12
                        or any(
                            not isinstance(i, str) or i not in seen for i in citations
                        )
                    ):
                        raise ValueError("模型引用了本轮未读取的记忆。")
                    return {
                        "status": "completed",
                        "message": required_text(arguments["message"]),
                        "citations": citations,
                        "actions": [],
                        "trace": trace,
                    }
                if name == "propose_changes":
                    if set(arguments) != {"summary", "actions"}:
                        raise ValueError("变更字段无效。")
                    actions = self.tools.validate_actions(project, arguments["actions"])
                    return {
                        "status": "pending",
                        "message": required_text(arguments["summary"]),
                        "citations": [],
                        "actions": actions,
                        "trace": trace,
                    }
                if name not in {"search_memory", "read_source"}:
                    raise ValueError("模型请求了不支持的工具。")
                if name == "read_source" and arguments.get("memory_id") not in seen:
                    raise ValueError("请先搜索记忆，再读取已检出的来源。")
                result = self.tools.read(project, name, arguments)
                seen.update(result.get("memory_ids", []))
                trace.append(
                    {
                        "step": name,
                        "label": "已检索相关记忆"
                        if name == "search_memory"
                        else "已核对记忆来源",
                        "memory_ids": result.get("memory_ids", []),
                    }
                )
            except (ValueError, KeyError, TypeError) as exc:
                # Model mistakes can be corrected within the same bounded run.
                result = {"error": str(exc)[:300]}
                trace.append(
                    {"step": "tool_error", "label": "工具参数未通过校验，要求模型修正"}
                )
                call_id = (
                    str(call.get("id", "invalid"))[:200]
                    if isinstance(call, dict)
                    else "invalid"
                )
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        raise ModelError(
            "已达到本轮 6 次模型调用上限，尚未形成有效结果。请缩小任务后重试。"
        )
