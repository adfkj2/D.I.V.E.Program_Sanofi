# 10 Roadmap

| 阶段 | Objective / Tasks | Deliverables | Dependencies / Risks | Acceptance / Tests | 复杂度 |
|---|---|---|---|---|---|
| 0 Research | 资料、威胁模型、taxonomy、ADR | docs、gold cases | benchmark 可比性 | 研究审阅完成 | M |
| 1 Evaluation Harness | adapters、gold evidence、judge、cost/latency logging | baseline runner | judge 偏差 | A–D 可复现 | M |
| 2 Event Store | immutable events、outbox、replay、retention | migrations/API | schema 演化 | 幂等/回放 | M |
| 3 Extraction | candidate、gate、entity/time、sensitivity | worker contracts | hallucinated extraction | provenance/拒写 | H |
| 4 Memory Store | version、relations、profile、indexes | structured projections | 版本冲突 | current/history fixtures | H |
| 5 Hybrid Retrieval | planner、RRF、rerank、packing | retrieve API | latency/noise | recall/precision/p95 | H |
| 6 Temporal | bitemporal query、as-of、timeline | time query API | vague dates/time zones | interval suite | H |
| 7 Consolidation | merge/reinforce/decay/archive、sleep jobs | jobs + dry-run | over-generalization | pollution/false memory | H |
| 8 User Control | view/edit/delete/export/disable | UI/API, tombstones | deletion propagation | end-to-end forget | M |
| 9 Benchmark | LoCoMo/LongMemEval/BEAM/LME-V2 subset、internal lifecycle | release report | dataset/license/cost | fixed config report | M |
| 10 Optimization | scale, read replicas, Qdrant/OpenSearch/graph trials、SelfMem-style strategy experiments | ADR updates | premature complexity | measured benefit | H |

每阶段都要求：objective、任务拆分、deliverable、依赖、风险、acceptance criteria、tests 和可观测性；未通过前一阶段门槛不得自动进入下一阶段。
