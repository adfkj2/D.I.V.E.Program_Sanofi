# Oracle 前沿方案选择与零付费验证（2026-09-23）

## 结论与边界

本课题应继续采用“事件源 + 带时效和来源的派生记忆 + 混合检索 + 有界证据阅读”，而不是直接替换为某个开源记忆框架，也不是把全部 Oracle haystack 交给长上下文模型。当前最有证据支撑的**记忆侧**配置是 semantic write gate、`document-admission=all` 与 `scoring-policy=balanced` 的组合；`all + legacy` 现已被 runner 拒绝。新的读侧 v3 增加“所问具体事实的前提是否受证据支持”检查和一次格式重试，但**尚无真实模型配对实验，不能声称 v3 提分**。历史 v2 可通过 `--reader-policy evidence_first_v2` 选择，便于对照。

本文件不是官方成绩。手册 C.3 指定的 40 分成绩，只有项目组用公布的 `evaluate_qa.py` 和统一评判模型复跑后才成立。DeepSeek 自评只可用于研发诊断。

## 对比的前沿路线与本题取舍

| 路线 | 对 LongMemEval Oracle 可能有用的机制 | 主要代价或约束风险 | 本项目决策 |
|---|---|---|---|
| 全量长上下文 / naive RAG | 实现简单，可能在较短历史上答得好 | 手册 F.1 明确禁止以全量 haystack 绕过记忆机制；成本高，无法证明写入、更新、遗忘 | 仅作诊断基线，不作提交系统 |
| Mem0 类作用域记忆与混合检索 | user/session/agent scope、多信号召回和实体连接适合跨会话 QA | 平台自报分数与本地 OSS、reader、top-k 和 judge 不同，不能横向引用；直接套框架也有 F.1 风险 | 借鉴作用域、BM25/semantic/实体融合，保留独立实现 |
| Letta / MemGPT 分层记忆 | 核心记忆与归档分层，版本和后台整理有利于长期交互 | Agent 自主修改原事实会增大错误写入和审计难度；本基准不奖励框架名称 | 借鉴可审阅的派生记忆，不赋予模型无约束改写权限 |
| Zep / Graphiti 时态知识图 | 有效时间窗、失效而不删、provenance 对 update/temporal/multi-hop 有针对性 | 图引擎和实体对齐增加写入成本与工程复杂度；Oracle 证据窗口较短，未证明图遍历收益 | 先以事件、关系表、有效窗口实现语义；仅在多跳召回实测不足时试图引擎 |
| A-MEM 动态链接 / 记忆演化 | 关联 note 可扩大跨会话召回，整理可压缩上下文 | LLM 生成链接和自动改写可能传播错事实，需成对消融、版本回溯 | 作为下一阶段实验，不直接改默认写入链 |
| 本项目 bounded evidence reader | 在 `limit=10`、`token_budget=1500` 内给出引用；可核对每条答案来源 | v2 “相关即必须答”使不可答题纪律变差；v3 尚未付费验证 | 保留硬引用和小上下文，新增特定事实支持检查、失败可追溯重试 |

上述比较依据现有研究梳理 `docs/01-research-landscape.md` 与原始项目/论文链接；外部系统各自数据、模型、预算和评判器不同，**没有**在本仓库做同条件端到端横评。尤其不能把厂商公开的 LongMemEval 数字写成本项目结果。

参考： [LongMemEval](https://github.com/xiaowu0162/LongMemEval)、[Mem0](https://github.com/mem0ai/mem0)、[Mem0 benchmark harness](https://github.com/mem0ai/memory-benchmarks)、[Letta](https://github.com/letta-ai/letta)、[Graphiti](https://github.com/getzep/graphiti)、[A-MEM](https://arxiv.org/abs/2502.12110)。

## 用本地证据决定先做什么

1. **正确的时间可见性优先于更复杂的检索。** Oracle 斜杠格式的“问题日期”是日精度，原检索却以当日随机钟点作截止。42 题共有 62 个标注证据轮落在当日晚些时候。新逻辑将当日结束时刻用于加载、ingest 过滤、检索及读侧时间脚手架；ISO 时间仍保留瞬时精度，次日轮不可见。
2. **优先保证 gold 进入有界上下文。** 在受影响的 39 道可回答题上，保存的 D 臂旧 trace 与当前离线 deterministic-reader 诊断比较，任一 gold 被检索从 21/39 到 34/39；当日晚些时候的 gold 被检索从 1/39 到 30/39，进入阅读包从 1/39 到 28/39。详见 `eval/reports/oracle-reanalysis-20260923/temporal-affected-42.json`。这不是同一提交代码/reader 的因果实验，更不是 QA 分数；另外 3 题是不可答题，不计入这些分母。
3. **文档准入必须配套重排。** 旧 B→D 只换 `legacy`→`balanced`，旧 exact-v1 自评从 0.416 到 0.434；两个 DeepSeek 判分口径方向相同，但存在判分失败数不等和统计功效不足。不能宣称已证明真实净效应。覆盖层可核对的机制是短证据不再被长文档挤出。当前 runner 阻止 `all + legacy`，保留 `off + legacy` 供消融。
4. **不可答题需要答案性判断，而非提高词面阈值。** D 臂旧非官方 judge 在 30 道不可答题上只判对 15 道；多数错误回答的证据相关度代理值仍远高于阈值 0.20。调这个阈值不能解决“话题相关 ≠ 所问值有据”。v3 改的是普适 reader 指令：允许多证据合成，但必须核对所需事实或推导前提；缺失则拒答。v2 保留作对照，v3 应先跑固定 20/50 题配对 pilot，特别检查不可答题和可回答题的召回/正确率权衡。
5. **格式失败用有限重试，不伪装成语义拒答。** 历史 D 臂 500 题中 7 个 `INVALID`。v3 对无效 JSON/schema 最多重试一次，保留每次错误、调用次数、用量和最后状态；重试传输失败仍显式标成错误，不报作正确拒答。这只是可靠性修复，不保证 7 题都会恢复。

## 评估口径更正

历史 `evaluation-exact.json` 使用了“参考词集合被假设包含”规则，会把否定参考答案的句子也判对；不应与修正版 exact-v2 比较绝对值。修正版对**同一历史提交**重新离线判分（仅作为字符串级 wiring check，非 LLM 判分）：

| 历史提交 | 原 reader 配置 | exact-v2 full-set | exact-v2 answered-only | 有效提交 |
|---|---|---:|---:|---|
| A | v1 / admission off / legacy | 0.242 | 0.3837 | 500/500 |
| B | v2 / admission all / legacy | 0.272 | 0.2962 | 500/500 |
| D | v2 / admission all / balanced | 0.290 | 0.3111 | 500/500 |

原 exact-v1 的 A/B/D 0.348/0.416/0.434 与上表不是同一判分器，不能拼接成趋势。原 DeepSeek rubric 自评 D=309/500=0.618 保留为**历史非官方读数**，但它属于旧时间逻辑、v2 reader、`dirty: true` 且运行状态 `PARTIAL`（493 正常、7 无效），不代表当前代码。旧 `answered_only_accuracy` 把正确拒答放进分子却排除出分母，旧按题型 `accuracy` 又只除以成功判题数；二者已经修正。报告同时提供 full-set 与 judged-only 分母，无 judge 时准确率为 `null`。`--judge-official` 被禁止，本地不能自称官方。

## 下一阶段必须做的配对实验

在取得 reader 额度后，固定同一 dataset SHA、模型版本、参数、gate、admission、scoring、top-k 和预算，仅改变 `--reader-policy evidence_first_v2` / `evidence_first`，分层抽样覆盖 30 道 `_abs`、temporal、update、assistant 与 preference。先 20 题检查结构化输出与失败，再 50 题比较答案性和成本，最后选一个配置从 **clean commit** 重新跑 500 题。可进一步单独测试本地语义 dense embedder；它与写入 gate 是两个不同模型，历史 D 的 dense provider 是 deterministic/non-semantic，不能假装已有 semantic retrieval 收益。任何新臂都用独立目录，不在旧 manifest 上 `--force` 覆写。

项目组的 `evaluate_qa.py`、指定评判模型及正式复跑目前不可得；因此正式 Overall Accuracy、40 分和最终名次均为 `NOT_EVALUATED`。README 提供当前 CLI 配置与 validator 命令。AI 辅助代码须由参赛者自行理解、复核并能答辩解释，遵守手册 F.2/F.3 的原创与可追溯要求。
