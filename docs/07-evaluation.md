# 07 Evaluation

## 评测原则

先建立可重复 harness，再增加 memory 功能。每个结果记录 commit、数据版本、模型、embedding、judge、top-k、上下文 token、并发、温度、成本和延迟。vendor-reported 数字只放参考列。

## 指标

| 维度 | 指标 | 测法 |
|---|---|---|
| Recall | evidence recall@k、memory recall | gold evidence 是否进入候选；区分 turn/session/memory |
| Precision | precision@k、context usefulness | 人工/LLM judge 标注候选是否支持答案 |
| Temporal | current/historical accuracy、interval accuracy | current、as-of、before/after、first/latest 问题 |
| Update | knowledge-update accuracy | 新事实后 current 正确、旧事实 historical 可回溯 |
| Multi-hop | answer exactness/support | 多个 source 是否全部找齐且关系正确 |
| Conflict | contradiction resolution | 不同有效区间和冲突证据的选择/并列 |
| Abstention | abstention precision/recall | gold 无事实时拒答；误拒和幻觉分别计 |
| False memory | false-memory rate | 生成 memory 不得超出 source event 的事实闭包 |
| Pollution | quality vs memory count | 同一用户 100/1K/10K/100K/1M events 的曲线 |
| System | p50/p95/p99、LLM/embedding calls、tokens、storage | ingest/retrieve/consolidate 分开计时 |

## Baselines

A 全量会话历史；B 纯 vector RAG；C summary+vector；D structured memory（无图）；E 本项目 hybrid memory。所有 baseline 使用同一 reader/judge 和 token budget；若某 baseline 不支持删除/时间，明确标记能力缺失。

## 数据集和内部场景

先跑 LoCoMo、LongMemEval cleaned、BEAM 128K subset，随后 LongMemEval-V2 small。内部 benchmark 按 Day 1/7/30/Month 3/12 构造：位置迁移、偏好变化、项目/目标、关系变化、临时计划、永久事实、用户纠正和删除。每个案例保留 gold event ids、答案、时间范围、应拒答标签。

## 评测防泄漏

memory writer 只能看到在线到当前问题时间的事件；judge 不参与检索；答案器只看到 context builder 输出。随机化问题顺序，区分 offline re-index 和 online ingest。厂商平台、OSS、不同 judge/model 不放在同一无注释排行榜。

## 验收门槛（初版假设，需实验校准）

- provenance coverage ≥ 99% 的长期 memory 有可追溯 source。
- false-memory rate = 0（内部 gold）为发布门槛；允许 inference 但必须显式标记。
- knowledge-update 与 historical recall 不能通过牺牲另一个达到；分别设基线 + 目标改善。
- p95 retrieve 在 10K memories/user、预算 1,500 tokens 下低于 500ms（本地 PostgreSQL 目标，非已验证结果）。
- 删除后 active retrieval 0 hits，且派生对象传播测试通过。
