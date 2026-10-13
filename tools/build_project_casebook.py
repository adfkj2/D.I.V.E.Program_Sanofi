"""Freeze a manually curated project case from exact repository excerpts.

This build/check tool reads documentation. The installed application reads only
the resulting package resource, never docs, ops, or evaluation output folders.
No Oracle questions, answers, model calls, or generated answers are included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/dive_memory/data/project-casebook.json"
NOTICE = (
    "仓库真实记录，人工整理映射；不是自动抽取或用户验证。"
    "本包为选摘快照，只代表已整理记录，不覆盖完整项目历史。"
)
TIME_NOTICE = (
    "来源只可靠地记录到日期。observed_at 按 Asia/Shanghai 当日日末归一化"
    "（day-end normalized），不是实际事件时分秒；案例截至 2026-10-12。"
    "完整开发成绩已核对 10 月 8—12 日的日末状态，不声称还原日内顺序。"
)

# These are human-authored record mappings, not question-to-answer rules.
# Anchors select the complete matching original paragraph/line verbatim.
SPECS = [
    {
        "id": "r26-full",
        "key": "最新完整开发自评",
        "value": "R26：422/500（84.4%），非官方开发自评。",
        "category": "result",
        "reason": "R26 全量确认结果；日期归一化后可回看这个旧版本。",
        "date": "2026-10-08",
        "path": "docs/oracle/24-asserted-detail-anchors-20261008.md",
        "anchor": "**最终结果（2026-10-08）：",
    },
    {
        "id": "r26-cost",
        "key": "累计开发费用",
        "value": "截至 R26，保守累计 ¥23.408646；累计授权 ¥30。",
        "category": "result",
        "reason": "这是多轮开发累计，不是 R26 单轮或一次冷启动成本。",
        "date": "2026-10-08",
        "path": "docs/oracle/24-asserted-detail-anchors-20261008.md",
        "anchor": "新增593次请求（阅读474、判分119）",
    },
    {
        "id": "r27-screen",
        "key": "R27 筛选结果",
        "value": "73/80（91.25%），重复使用的开发筛选集；不能代表 500 题全量成绩。",
        "category": "result",
        "reason": "91.25% 由 73÷80 确定性换算；分母与 R40 的 500 题不同。",
        "date": "2026-10-09",
        "path": "docs/oracle/25-precise-modality-terms-20261008.md",
        "anchor": "固定80题由R26的71/80提高至",
    },
    {
        "id": "r27-decision",
        "key": "R27 推广决定",
        "value": "不推广、不追加全量；可答净增 2，未达到预设净增 3 的门槛。",
        "category": "decision",
        "disposition": "rejected",
        "reason": "结果提高不等于已采用；该轮记录明确保留推广门槛。",
        "date": "2026-10-09",
        "path": "docs/oracle/25-precise-modality-terms-20261008.md",
        "anchor": "固定80题由R26的71/80提高至",
    },
    {
        "id": "r36-full",
        "key": "最新完整开发自评",
        "value": "R36：439/500（87.8%），非官方开发自评；可答 411/470、不可答 28/30。",
        "category": "result",
        "reason": "10 月 11 日日末已完成全量；更新 R26 日末快照。当天更早的 R33 结果未单独展开，不还原日内顺序。",
        "date": "2026-10-11",
        "path": "docs/oracle/34-temporal-refusal-review-20261011.md",
        "anchor": "## 全量结果：439/500（87.8%），无退步",
        "extra_anchor": "500题全部判分，相对R33改善13/退步0",
        "replaces_key": "最新完整开发自评",
        "replaces_id": "r26-full",
    },
    {
        "id": "r40-full",
        "key": "最新完整开发自评",
        "value": "R40：451/500（90.2%），非官方开发自评；可答 423/470、不可答 28/30。",
        "category": "result",
        "reason": "明确更新 R36 日末全量快照；不把筛选、工程测试或缓存回放当作新准确率。",
        "date": "2026-10-12",
        "path": "docs/oracle/38-current-state-review-20261012.md",
        "anchor": "2026-10-12，**451/500（90.2%）**",
        "replaces_key": "最新完整开发自评",
        "replaces_id": "r36-full",
    },
    {
        "id": "r40-status",
        "key": "官方成绩",
        "value": "未评估（NOT_EVALUATED）；90.2% 不能称为官方成绩、盲测成绩或泛化准确率。",
        "category": "note",
        "reason": "同一公开开发集、同家族 judge，混合缓存与新阶段生成；没有新的独立 500 题采样。",
        "date": "2026-10-12",
        "path": "docs/oracle/38-current-state-review-20261012.md",
        "anchor": "本结果是同一公开开发集",
    },
    {
        "id": "r40-cost",
        "key": "累计开发费用",
        "value": "截至 R40，保守累计 ¥31.456754；累计授权 ¥40，规划余 ¥7.543246。",
        "category": "result",
        "reason": "更新 R26 的历史累计值；只表示报告当时的费用快照，不是实时余额。",
        "date": "2026-10-12",
        "path": "docs/oracle/38-current-state-review-20261012.md",
        "anchor": "全量阶段新增8阅读+2判分=10请求",
        "replaces_key": "累计开发费用",
        "replaces_id": "r26-cost",
    },
    {
        "id": "r40-round-cost",
        "key": "R40 本轮费用",
        "value": "含筛选共新增 16 次请求，保守增量 ¥0.276516。",
        "category": "result",
        "reason": "单轮增量与累计开发费用分别记录，不能互相替代。",
        "date": "2026-10-12",
        "path": "docs/oracle/38-current-state-review-20261012.md",
        "anchor": "全量阶段新增8阅读+2判分=10请求",
    },
    {
        "id": "cold-cost",
        "key": "独立冷启动费用",
        "value": "尚未实测；¥31.456754 和 ¥0.276516 都不是独立冷启动完整 500 题成本。",
        "category": "note",
        "reason": "不从累计开发费用或混合阶段计时推测一次冷运行成本。",
        "date": "2026-10-12",
        "path": "docs/oracle/39-compliance-audit-20261012.md",
        "anchor": "这支持“固定策略下复用相同请求结果”的解释",
    },
    {
        "id": "rule-review",
        "key": "规则审查",
        "value": "尚未全部通过；公开 500 题多轮用于分析和策略选择，F.1.4 过拟合调参风险未关闭。",
        "category": "note",
        "reason": "没有发现按题号填答案，不等于项目组已认可合规。",
        "date": "2026-10-12",
        "path": "docs/oracle/39-compliance-audit-20261012.md",
        "anchor": "现有结果不是空口宣称",
    },
    {
        "id": "r42-delivery",
        "key": "R42 工程交付",
        "value": "共用运行时、CLI/API、SQLite 持久化与四视图工作台；确认、提议、拒绝和冲突分别保留。",
        "category": "result",
        "reason": "明确字段录入仍由人确认；自然文本使用启发式抽取，不能称为自动理解所有项目文档。",
        "date": "2026-10-12",
        "path": "docs/oracle/40-engineering-delivery-20261012.md",
        "anchor": "新增应用用例层、明确业务记录 schema",
    },
    {
        "id": "r42-performance",
        "key": "R42 查询性能",
        "value": "1 万条记录查询 p50 从 4783ms 降至 522ms；同机器、同输入，不是模型延迟或并发 SLA。",
        "category": "result",
        "reason": "后值来自来源批量化后的 30 问/规模测量，前值来自同报告的性能修复前登记。",
        "date": "2026-10-12",
        "path": "docs/oracle/40-engineering-delivery-20261012.md",
        "anchor": "| 来源批量化后查询 |",
        "extra_anchor": "执行中补充（性能修复前登记）：",
    },
    {
        "id": "value-evidence",
        "key": "用户价值验证",
        "value": "构造结构化任务 120/120，完整事件扫描基线也为 120/120；尚不能据此证明真实用户效率提高。",
        "category": "note",
        "reason": "需要继续比较正确恢复时间及维护负担，没有参与者观察结果。",
        "date": "2026-10-12",
        "path": "docs/oracle/40-engineering-delivery-20261012.md",
        "anchor": "| 结构化任务 |",
        "extra_anchor": "结果未以测试条数换算手册分数。",
    },
    {
        "id": "next-work",
        "key": "下一步工作",
        "value": "仍待完成项目组规则判断、正式统一 judge、真实用户观察、独立自然语言保留集，以及真实冷生成与费用结算。",
        "category": "decision",
        "disposition": "proposed",
        "reason": "这些是报告列出的未完成事项；本案例不会把待办标为已经执行。",
        "date": "2026-10-12",
        "path": "docs/oracle/40-engineering-delivery-20261012.md",
        "anchor": "仍待外部完成：",
    },
]


def source_line(lines: list[str], anchor: str) -> tuple[int, str]:
    matches = [
        (index + 1, line) for index, line in enumerate(lines) if line.startswith(anchor)
    ]
    if len(matches) != 1:
        raise ValueError(f"source anchor must match exactly once: {anchor!r}")
    return matches[0]


def build(frozen_at: str) -> dict:
    records = []
    for spec in SPECS:
        source_bytes = (ROOT / spec["path"]).read_bytes()
        lines = source_bytes.decode("utf-8-sig").splitlines()
        selected = [source_line(lines, spec["anchor"])]
        if spec.get("extra_anchor"):
            selected.append(source_line(lines, spec["extra_anchor"]))
        selected.sort()
        records.append(
            {
                "id": spec["id"],
                "key": spec["key"],
                "value": spec["value"],
                "category": spec["category"],
                "disposition": spec.get("disposition", "confirmed"),
                "reason": spec["reason"],
                "session": "repo-casebook-" + spec["id"],
                "observed_at": spec["date"] + "T23:59:59+08:00",
                "date_precision": "day-end normalized; Asia/Shanghai",
                "source_title": lines[0].removeprefix("# "),
                "source_path": f"{spec['path']}:{selected[0][0]}",
                "source_lines": [line for line, _ in selected],
                "source_excerpt": "\n\n".join(text for _, text in selected),
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "source_lf_sha256": hashlib.sha256(
                    source_bytes.replace(b"\r\n", b"\n")
                ).hexdigest(),
                **{
                    key: spec[key]
                    for key in ("replaces_key", "replaces_id")
                    if key in spec
                },
            }
        )
    return {
        "schema": "dive-project-casebook-v1",
        "id": "dive-research-history-20261012-v2",
        "title": "本项目的真实研发交接：分数、决定与尚未完成的工作",
        "notice": NOTICE,
        "frozen_at": frozen_at,
        "as_of_date": "2026-10-12",
        "time_notice": TIME_NOTICE,
        "source_hash_scope": "SHA-256 of the original file bytes at frozen_at, not just the excerpt",
        "source_lf_hash_scope": "SHA-256 of entire source with CRLF normalized to LF; permits Git checkout newline conversion only",
        "published": False,
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify frozen records against current sources",
    )
    args = parser.parse_args()
    if args.check:
        existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
        current = build(existing["frozen_at"])
        newline_only = 0
        for frozen_row, current_row in zip(
            existing["records"], current["records"], strict=True
        ):
            if (
                frozen_row["source_sha256"] != current_row["source_sha256"]
                and frozen_row["source_lf_sha256"] == current_row["source_lf_sha256"]
            ):
                # Keep the frozen original-byte hash. Only a checkout's newline
                # conversion is compatible; arbitrary content changes still fail.
                current_row["source_sha256"] = frozen_row["source_sha256"]
                newline_only += 1
        if existing != current:
            raise SystemExit(
                "Casebook source or mapping drift: inspect changes before rebuilding."
            )
        print(
            f"Verified {len(existing['records'])} curated records and full-source content; "
            f"{newline_only} source mappings matched after LF normalization only."
        )
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frozen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = build(frozen_at)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Built {len(payload['records'])} curated records: {OUTPUT.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
