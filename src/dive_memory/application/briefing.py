"""Deterministic project resumption over explicit records, never an LLM answer.

The small supported vocabulary selects tasks or exact record names. It does not
infer missing facts, semantic entailment, adoption, or intent from arbitrary prose.
"""

from typing import Any

from ..models import utc_now
from ..temporal import parse_instant, require_instant

Row = dict[str, Any]
DISPOSITION_LABELS = {
    "confirmed": "已确认",
    "proposed": "仅提议",
    "rejected": "未采用",
}
SECTIONS = (
    ("decisions", "已确认的决定", "decision"),
    ("constraints", "当前约束", "constraint"),
    ("results", "已记录的结果", "result"),
    ("notes", "已知事实与边界", "note"),
    ("preferences", "协作偏好", "preference"),
)
TASKS = {
    "resume": ("继续项目", "继续工作", "当前状态", "恢复项目", "项目简报", "resume"),
    "constraints": ("当前约束", "有哪些约束", "constraints"),
    "decisions": ("当前决定", "已确认的决定", "已采纳方案", "decisions"),
    "proposed": ("待确认事项", "有哪些提议", "未完成事项", "proposals"),
    "rejected": ("哪些方案未采用", "未采用原因", "未采用的方案", "rejected"),
    "changes": (
        "上次以来有什么变化",
        "上次以来变化",
        "查看变化",
        "有什么变化",
        "变化",
        "changes",
    ),
}
CASE_NOTICE = (
    "案例截至 2026-10-12，为人工整理的选摘资料，不覆盖完整日志。"
    "源日期按 Asia/Shanghai 日末归一化，不代表真实事件时分秒。"
)


def data(row: Row) -> Row:
    return row["structured_content"]


def explicit(rows: list[Row]) -> list[Row]:
    return [row for row in rows if data(row).get("schema") == "project-record-v1"]


def quoted(value: str) -> str:
    """Keep multiline supplied text visibly inside the exported source block."""
    return "\n".join("> " + line for line in value.splitlines())


def describe(row: Row) -> str:
    value = data(row)
    label = DISPOSITION_LABELS[value["disposition"]]
    reason = f"；依据：{value['reason']}" if value.get("reason") else ""
    return f"{value['key']}：[{label}] {value['value']}{reason}"


def citations(rows: list[Row]) -> list[Row]:
    unique = {row["id"]: row for row in rows}
    return [
        {
            "memory_id": row["id"],
            "quote": data(row)["value"],
            "key": data(row)["key"],
            "disposition": data(row)["disposition"],
        }
        for row in unique.values()
    ]


def build_brief(state: Row, *, since: str | None = None) -> Row:
    end = state["as_of"] or utc_now()
    start = require_instant(since, "since")
    if start and parse_instant(start) > parse_instant(end):
        raise ValueError("since must not be later than the selected viewing time")
    records = explicit(state["memories"])
    groups: dict[str, list[Row]] = {}
    for row in records:
        if data(row)["disposition"] == "confirmed":
            groups.setdefault(data(row)["key"], []).append(row)
    conflicts = [
        {"key": key, "items": rows}
        for key, rows in groups.items()
        if len({data(row)["value"] for row in rows}) > 1
    ]
    conflict_keys = {group["key"] for group in conflicts}
    confirmed = [
        row
        for row in records
        if data(row)["disposition"] == "confirmed"
        and data(row)["key"] not in conflict_keys
    ]
    sections = [
        {
            "id": key,
            "title": title,
            "items": [row for row in confirmed if data(row)["category"] == category],
        }
        for key, title, category in SECTIONS
    ]
    sections.extend(
        {
            "id": key,
            "title": title,
            "items": [row for row in records if data(row)["disposition"] == key],
        }
        for key, title in (("proposed", "待确认与计划"), ("rejected", "未采用及原因"))
    )
    timeline = [
        row
        for row in explicit(state["timeline"])
        if parse_instant(row["observed_at"]) <= parse_instant(end)
    ]
    by_id = {row["id"]: row for row in timeline}
    changes = []
    if start:
        for row in sorted(
            timeline, key=lambda item: parse_instant(item["observed_at"])
        ):
            if parse_instant(row["observed_at"]) <= parse_instant(start):
                continue
            predecessor = row.get("supersedes_id")
            changes.append(
                {
                    "kind": "updated" if predecessor else "added",
                    "before": by_id.get(predecessor),
                    "after": row,
                }
            )
    notices = [
        "基于当前项目的已整理记录；仅使用明确字段，未录入的信息不包含在本简报中。",
        "提议不是已采纳决定，冲突需要人工核对；本简报不调用模型。",
        "变更比较只列仍可见的新增与显式替换，不能恢复已删除的来源。",
    ]
    if any(
        data(row).get("session", "").startswith("repo-casebook-") for row in timeline
    ):
        notices.append(CASE_NOTICE)
    result: Row = {
        "mode": "local_records",
        "namespace": state["namespace"],
        "as_of": state["as_of"],
        "viewed_at": end,
        "since": start,
        "summary": f"根据 {len(records)} 条明确记录恢复项目：{len(confirmed)} 条确认记录，"
        f"{len(conflicts)} 组待核对冲突。未录入的信息不包含在内。",
        "sections": sections,
        "conflicts": conflicts,
        "changes": changes,
        "notices": notices,
        "unstructured_records_excluded": len(state["memories"]) - len(records),
    }
    result["markdown"] = handoff(result)
    return result


def handoff(brief: Row) -> str:
    lines = [
        "# 项目交接简报",
        "",
        f"项目：{brief['namespace']}",
        f"查看时点：{brief['viewed_at']}",
        "",
        brief["summary"],
        "",
    ]
    lines.extend("- " + notice for notice in brief["notices"])
    sources: dict[str, Row] = {}
    for section in brief["sections"]:
        if not section["items"]:
            continue
        lines.extend(["", "## " + section["title"], ""])
        for row in section["items"]:
            lines.extend([quoted(describe(row)), f"来源记录：{row['id']}", ""])
            sources[row["id"]] = row
    if brief["conflicts"]:
        lines.extend(["", "## 待核对冲突（不选定其中一个值）", ""])
        for group in brief["conflicts"]:
            for row in group["items"]:
                lines.extend([quoted(describe(row)), f"来源记录：{row['id']}", ""])
                sources[row["id"]] = row
    if brief["since"]:
        lines.extend(["", "## 期间变化", "", f"比较起点：{brief['since']}"])
        if not brief["changes"]:
            lines.append("没有可见的新增或显式替换记录。")
        for change in brief["changes"]:
            old, new = change["before"], change["after"]
            if old:
                lines.extend(["此前：", quoted(describe(old))])
                sources[old["id"]] = old
            lines.extend(
                ["此后：", quoted(describe(new)), f"来源记录：{new['id']}", ""]
            )
            sources[new["id"]] = new
    lines.extend(["", "## 来源索引", ""])
    for identifier, row in sources.items():
        record = data(row)
        lines.extend([f"### {identifier}", f"记录时间：{row['observed_at']}"])
        for field, label in (
            ("source_title", "文档"),
            ("source_path", "位置"),
            ("source_sha256", "冻结原文件 SHA-256"),
        ):
            if record.get(field):
                lines.append(f"{label}：{record[field]}")
        if record.get("source_excerpt"):
            lines.extend(
                [
                    "保存的原文摘录（人工整理，非实时核验）：",
                    quoted(record["source_excerpt"]),
                ]
            )
        else:
            lines.extend(["用户记录：", quoted(row["content"])])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def normalized(text: str) -> str:
    return "".join(text.casefold().strip().rstrip("？?。.!！").split())


def consult_records(state: Row, question: str, *, since: str | None = None) -> Row:
    if not question.strip() or len(question) > 4000:
        raise ValueError("question must contain 1 to 4000 characters")
    brief = build_brief(state, since=since)
    records = explicit(state["memories"])
    query = normalized(question)
    key_query = query
    explicit_lookup = False
    for prefix in ("核查", "查询", "查找"):
        if query.startswith(prefix):
            key_query = query[len(prefix) :].lstrip(":：")
            explicit_lookup = True
            break
    selected = [row for row in records if normalized(data(row)["key"]) == key_query]
    intent = (
        "lookup"
        if selected or explicit_lookup
        else next(
            (
                name
                for name, aliases in TASKS.items()
                if query in {normalized(alias) for alias in aliases}
            ),
            "unsupported",
        )
    )
    status, reason = "ANSWER", "explicit_records"
    conflict_keys = {group["key"] for group in brief["conflicts"]}
    if intent == "unsupported":
        status, reason = "UNSUPPORTED", "unsupported_question"
        answer = "尚不支持这个自由问题。请选择项目任务，或输入完整记录名称；不会从相似内容猜测结论。"
    elif intent == "changes":
        selected = []
        if not since:
            status, reason = "ABSTAIN", "comparison_time_required"
            answer = "请先指定上次工作的时间，才能核查此后的变化。"
        else:
            parts = []
            for change in brief["changes"]:
                if change["before"]:
                    parts.append("此前：" + describe(change["before"]))
                    selected.append(change["before"])
                parts.append("此后：" + describe(change["after"]))
                selected.append(change["after"])
            answer = (
                "\n".join(parts)
                or "指定区间没有可见的新增或显式替换记录；不包含已删除来源。"
            )
    else:
        if intent != "lookup":
            selected = [
                row
                for section in brief["sections"]
                if intent == "resume" or section["id"] == intent
                for row in section["items"]
            ]
            relevant = [
                group
                for group in brief["conflicts"]
                if intent == "resume"
                or any(
                    data(row)["category"]
                    == {"constraints": "constraint", "decisions": "decision"}.get(
                        intent
                    )
                    for row in group["items"]
                )
            ]
            selected.extend(row for group in relevant for row in group["items"])
        if not selected:
            status, reason = "ABSTAIN", "missing_record"
            answer = "没有找到对应的明确记录，保持未知。"
        elif any(data(row)["key"] in conflict_keys for row in selected):
            status, reason = "ABSTAIN", "conflict"
            answer = "存在未解决冲突，不能选定一个当前值。请核对来源：\n" + "\n".join(
                describe(row) for row in selected
            )
        else:
            answer = "\n".join(describe(row) for row in selected)
    return {
        "mode": "local_records",
        "status": status,
        "intent": intent,
        "reason": reason,
        "answer": answer,
        "citations": citations(selected),
        "namespace": state["namespace"],
        "as_of": state["as_of"],
        "since": brief["since"],
        "model_calls": 0,
        "notices": brief["notices"],
    }
