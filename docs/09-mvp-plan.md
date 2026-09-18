# 09 MVP Plan（4–6 周）

目标是可运行、可评测的单用户/多 agent memory service：事件溯源、选择性抽取、结构化事实、时间窗口、混合检索、来源、冲突和基础 consolidation。暂不做多模态、参数记忆、跨租户共享和独立图数据库。

| 周期 | 工程任务 | 交付物 | 依赖/风险 | 验收 |
|---|---|---|---|---|
| 1 | schema、migration、event append、outbox、scope/auth skeleton、gold fixtures | 可回放 event store + API contract | 时间语义误用 | 事件 immutable、幂等、审计可查 |
| 2 | candidate extraction schema、write gate、entity/time parser、source links | 事件→候选流水线 | LLM JSON 不稳定 | 低价值样本拒绝；每个 accepted 有 source |
| 3 | memories/version/relation/profile projection、pgvector/FTS indexing | structured memory store | duplicate/contradiction 边界 | Chengdu→Shanghai 和 Java→Python fixture 通过 |
| 4 | query planner、dense+sparse+metadata retrieval、RRF、rerank、context budget | retrieve API | 噪声和延迟 | current/history/multi-hop/abstention smoke set |
| 5 | consolidation、reinforce/decay/archive、delete propagation、export/view APIs | lifecycle jobs + user controls | 派生删除遗漏 | dry-run、软删、向量/FTS/cache 清理测试 |
| 6（可选） | benchmark harness、baseline A–E、load test、observability、runbook | 可复现报告和 MVP 发布候选 | judge 成本、数据许可 | 公开 subset + 内部 suite；p95 和 false-memory 门槛 |

## Definition of Done

每个 API 有 schema、权限、幂等和错误语义；所有 memory 能追溯 source；任何 projection 可从 events 重建；删除测试覆盖 vector/FTS/relation/cache；测试 fixture 覆盖时间、冲突、拒答；报告包含成本和 p95。

## 高级功能推迟

screen/multimodal、复杂 graph reasoning、agent 自主策略优化、parameter/activation memory、跨 agent marketplace 和自动微调。它们只有在 MVP 的错误分析证明收益后进入 roadmap。
