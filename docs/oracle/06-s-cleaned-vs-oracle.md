# `LongMemEval_S-cleaned` 与 Oracle 的区别

## 一句话

S-cleaned 是**记忆形成与检索阶段**的实验语料；Oracle 是**端到端问答**的正式计分基准。
两者不能互相替代，也不能用前者的指标冒充后者的分数。

## 逐项对照

| 项 | `LongMemEval_S-cleaned` | `longmemeval_oracle.json` |
|---|---|---|
| 文件大小 | ~277 MB | ~15.4 MB |
| 题量 | 500 case | 500 题 |
| 有无官方 `answer` | 无（不产出回答） | **有**（供 judge 打分） |
| 历史是否剪枝 | 完整长上下文 | 剪枝到证据 session |
| 主指标 | formation coverage / retrieval recall | **Overall Accuracy** |
| 计分 | 否 | **是（× 40）** |
| 加载器 | `eval_adapters.longmemeval` 直接加载 | `oracle.loader`（严格 9 字段 schema + 完整性） |
| 运行入口 | `longmemeval_benchmark.py` | `oracle.runner` |

## 为什么不能混用

1. S-cleaned 没有 `answer`，无法用 `evaluate_qa.py` 打分 → 不能产出 Oracle 分数。
2. S-cleaned 是长上下文语料，若直接塞进 reader 会绕过记忆机制（手册附录 F 禁止）。
3. Oracle 剪枝到证据 session，正是要考记忆系统的形成/检索/阅读三段能否端到端答对。

## 现状标注

- README 的 `longmemeval-report.md` 写的是 **v1 关键词门**（形成率 6.14%），**不是系统现状**。
- 系统现状：semantic 门形成率 82.48%（全量），但这是**检索阶段**指标，不是 Oracle 分数。
- 本仓库尚无官方 Overall Accuracy；`evaluate.py` 只能给自测分。
