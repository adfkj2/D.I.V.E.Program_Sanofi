"""Build the UI's labelled, static evidence view from actual saved measurements."""

import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "eval/reports/engineering-delivery-20261012"


def read(path):
    return json.loads((REPORTS / path).read_text(encoding="utf-8"))


def main():
    structured = read("structured-validation/summary.json")
    failures = read("structured-validation/failures.json")
    before, after = read("performance/summary.json"), read("performance-batched/summary.json")
    suite = ET.parse(REPORTS / "full-tests.xml").find("testsuite")
    assert suite is not None
    total, skipped = int(suite.attrib["tests"]), int(suite.attrib["skipped"])
    bad = int(suite.attrib["errors"]) + int(suite.attrib["failures"])
    aliases = {"memory": "完整记忆系统", "event_scan": "完整结构化事件扫描", "rolling_summary": "滚动字段摘要",
               "recent_session": "最近会话", "lexical_top_one": "词面首条检索代理",
               "without_updates": "去掉显式更新", "without_source_gate": "去掉来源角色门"}
    sections = [
        {"title": "公开 Oracle 开发成绩", "value": "451 / 500 · 90.2%",
         "description": "R40 历史非官方自评：同模型家族判分、混合精确缓存与新生成；全部 500 题曾用于开发，不是盲测。官方成绩 NOT_EVALUATED。规则 F.1.4 风险仍待项目组认定。",
         "columns": ["错误类别", "数量"], "rows": [["误拒",22],["误答",26],["无效输出",1]]},
        {"title": "工程检查", "value": f"{total-skipped-bad} 通过 · {skipped} 跳过",
         "description": "实际完整 Python 回归；跳过的 8 项 PostgreSQL 与 1 项真实语义模型检查不能视作通过。CI 配置已提供，远程运行状态不在此宣称。浏览器验证覆盖桌面和手机的真实操作。"},
        {"title": "Reader 提取一致性", "value": "500 / 500",
         "description": "独立工厂对历史可见输入和精确缓存的输出一致，排除计时。1110 次 reader 缓存命中，0 个新增模型请求。这不是新的答题成绩，也不是新生成或官方判分。"},
        {"title": "结构化机制对照", "value": "30 个故事 · 120 个任务",
         "description": "公开构造的开发验证，30 个领域复用 6 类机制模板；输入提供明确字段，不检验自然语言抽取或同义改写。事件扫描与完整系统并列，尚无真实用户效率优势证据。词面检索是代理，不是完整 LLM RAG。",
         "columns": ["方案", "正确", "任务数"], "rows": [[aliases[k],v["correct"],v["cases"]] for k,v in structured["arms"].items()]},
        {"title": "规模测试：批量读取来源后", "description": "同一机器和持久化输入，每规模 30 次问题；确定性非语义 embedding，无模型生成。前后非严格隔离负载实验；毫秒数是系统时间，不是用户恢复时间。",
         "columns": ["事件", "此前 p50 / p95 ms", "此后 p50 / p95 ms"],
         "rows": [[a["events"], f"{a['query']['p50_ms']:.1f} / {a['query']['p95_ms']:.1f}",f"{b['query']['p50_ms']:.1f} / {b['query']['p95_ms']:.1f}"] for a,b in zip(before["runs"],after["runs"],strict=True)]},
        {"title": f"检查全部 {len(failures)} 条基线与消融失败", "collapsed": True,
         "description": "完整系统在这组结构化任务中无失败；以下为所有对照方案未通过的任务，未挑选只对系统有利的示例。",
         "columns": ["方案 / 案例", "应得到", "实际得到"],
         "rows": [[aliases[r["arm"]]+" / "+r["story"]+" / "+r["task"], r["expected"] or "未知", r["actual"] or "未知"] for r in failures]},
        {"title": "尚未得到的外部证据", "description": "界面和工程检查不能替代下列事项。",
         "items": ["3–5 位目标用户的实际任务观察", "相对文档和结构化摘要的恢复时间及维护成本收益",
            "独立保管的自然语言保留集与同义改写验证", "项目组对 F.1.1 / F.1.4 的规则认定",
            "正式统一 judge 以及冻结版本的全新付费生成"]},
    ]
    output = {"provenance": "2026-10-12 R40/R42；docs/26-verification.md 与 eval/reports/engineering-delivery-20261012；本页为静态已测报告，不是当前会话的模型成绩", "sections": sections}
    (ROOT / "src/dive_memory/static/verification.json").write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__ == "__main__":
    main()
