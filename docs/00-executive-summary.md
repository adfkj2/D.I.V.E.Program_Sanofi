# Executive Summary

## 1. Recommended Architecture

以 immutable conversation/event store 为 source of truth，以 versioned MemoryObject 为可检索 projection。MVP 使用 PostgreSQL + pgvector + PostgreSQL FTS + 时态关系表；事件追加后由异步 worker 做候选抽取、write gate、实体解析、时间解析、冲突检测、版本写入和索引更新。检索由 query planner 按 current/history/episode/preference/procedure/multi-hop 选择 dense、BM25、metadata、entity/relation 通道，再做融合、重排、时间一致性检查和 context packing。

## 2. Key Design Decisions

1. Event 是 source of truth，MemoryObject 是可重建 projection。
2. 原始事件 immutable，历史事实不因新事实而删除。
3. 双时间语义：observed/created 与 valid_from/to 分开。
4. FACT、OBSERVATION、INFERENCE 强制区分并可追溯。
5. 写入选择性由规则 + 结构化 LLM gate 决定。
6. 默认异步抽取，显式“记住”可走受限即时路径。
7. Profile 只表达当前视图，collection/episode 保留历史。
8. 召回 query-dependent，混合检索先过滤权限/时间，再融合和重排。
9. Decay 降低优先级或转冷层，不用固定乘法自动抹掉重要事实。
10. 关系表先于独立图数据库；所有复杂组件须由 benchmark 和压测证明收益。

## 3. Ideas Borrowed

Letta：分层 memory、版本化 MemFS、后台 dreaming 和 review。Mem0：scope、ADD-first、多信号检索、实体链接和 benchmark harness。Graphiti：episode→entity→temporal fact、有效窗口和 provenance。A-MEM：可解释 note、标签和邻域链接。MIRIX：资源/工具轨迹与 consolidation 的分类视角。MemOS：统一元数据、版本链、调度和治理。LangMem：profile/collection/episodic/procedural API 与 active/background 双路径。

## 4. Deliberately Not Built Yet

不在 MVP 引入参数/activation memory、复杂多模态屏幕记忆、自动微调、跨租户 memory marketplace、独立 graph database、agent 自主无限修改和 SelfMem 式策略自优化。原因是它们会扩大安全、删除、评测和运维面；先通过失败分析证明需要。

## 5. MVP

4–6 周完成：conversation ingestion、immutable events、selective extraction、structured/temporal memory、provenance、hybrid retrieval、conflict handling、basic consolidation/decay、delete/export/view controls、evaluation harness 和 A–E baseline。

## 6. Evaluation Plan

LoCoMo、LongMemEval cleaned、BEAM subset、LongMemEval-V2 small 加内部 Day 1/7/30/Month 3/12 生命周期集。测 evidence recall/precision、current/historical、update、multi-hop、conflict、abstention、false-memory、pollution、p50/p95/p99、tokens/cost/storage。

## 7. Roadmap

Research → harness → event store → extraction → storage → hybrid retrieval → temporal → consolidation/decay → user control → benchmark → measured optimization。

## 8. Biggest Risks

最大风险是过度抽取、时间表达错误、冲突合并错误、删除传播遗漏、LLM judge 偏差、供应商 benchmark 不可比和多库双写复杂度。每项都在文档中有门槛或回放测试。

## 9. Open Questions

写入 gate 阈值、不同 embedding 的收益、profile 与 collection 的边界、图查询何时超过关系表、consolidation 频率、LLM rerank 的成本收益、1M events 分区与索引策略，必须用 harness 实验回答。

## 10. Repository Structure

```text
docs/                 research and design
docs/adr/             architecture decisions
src/api/              provider-neutral REST API
src/ingestion/        event append and write gate
src/memory/           projections, lifecycle, provenance
src/retrieval/        planner, channels, rerank, context
src/jobs/             consolidation, decay, reindex, deletion
src/storage/          postgres/vector/object adapters
tests/fixtures/       temporal/conflict/provenance gold cases
eval/                  benchmark adapters and reports
ops/                   migrations, compose, runbooks
```

## Final answers to core questions

- Unit：带 provenance、时间窗口和生命周期的 MemoryObject；Message/Event 是证据层。
- Truth：immutable Event Store。
- Long-term：稳定、可验证、未来可复用或用户明确要求保留的内容。
- Forget：ephemeral、过期计划、低价值闲聊和用户删除项；通过策略归档/删除。
- Change：追加新 event 和新 version，关闭旧 valid window，保留历史。
- False memory：source closure、gate、inference 标签、引用和 abstention。
- Current/history：query planner 的 as-of/current 过滤与版本并列。
- Consolidation：session 结束轻整理，空闲/每日重整理；全部可 dry-run/replay。
- Profile：只有稳定且 schema 明确的当前状态才更新。
- Graph：先关系表；以多跳质量和延迟实验证明后再引入。
- LLM 权限：提议和受限工具调用；不能改源事件、越权读写或无审计删除。
- 10K/100K/1M：先 PostgreSQL 分区/索引/异步化，达到实测瓶颈再拆存储，projection 可重建。
- 证明效果：固定配置、强制 baseline、gold provenance、分项指标、成本/延迟和长期 pollution 曲线。
