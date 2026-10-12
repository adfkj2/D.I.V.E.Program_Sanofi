"""Clearly synthetic scenario, created through the same public use cases."""

from .records import ProjectRecord
from .runtime import MemoryApplication


def load_demo(app: MemoryApplication, namespace: str) -> dict:
    rows = [
        (
            "预算上限",
            "20 元",
            "constraint",
            "confirmed",
            "启动公开检索实验",
            "2026-10-01T09:00:00+00:00",
        ),
        (
            "当前基线",
            "方案 A",
            "decision",
            "confirmed",
            "已采纳；延迟符合要求",
            "2026-10-01T09:10:00+00:00",
        ),
        (
            "方案 B 结果",
            "准确率 84%，延迟 3.2 秒",
            "result",
            "confirmed",
            "合成演示数值，不是实测",
            "2026-10-03T09:00:00+00:00",
        ),
        (
            "方案 B 采纳",
            "不采用",
            "decision",
            "rejected",
            "超出 2 秒延迟要求",
            "2026-10-03T09:10:00+00:00",
        ),
        (
            "预算上限",
            "40 元",
            "constraint",
            "confirmed",
            "明确调整项目预算",
            "2026-10-05T09:00:00+00:00",
        ),
        (
            "当前基线",
            "考虑方案 C",
            "decision",
            "proposed",
            "尚未评测，也未确认采用",
            "2026-10-05T09:10:00+00:00",
        ),
        (
            "阅读偏好",
            "先看简短结论，再打开证据",
            "preference",
            "confirmed",
            "用户明确选择",
            "2026-10-05T09:20:00+00:00",
        ),
    ]
    first_budget = None
    results = []
    for i, (key, value, category, disposition, reason, date) in enumerate(rows):
        result = app.record(
            namespace,
            ProjectRecord(
                key, value, category, disposition, reason, f"session-{i // 2 + 1}"
            ),
            idempotency_key=f"synthetic-demo-v1-{i}",
            observed_at=date,
            replaces=first_budget if i == 4 else None,
        )
        if i == 0:
            first_budget = result["memory_ids"][0]
        results.append(result)
    return {
        "synthetic": True,
        "namespace": namespace,
        "events": results,
        "notice": "公开合成演示；数值为构造数据，不是用户访谈或模型准确率。",
    }
