# 10 Roadmap

> **Phase 4 P0 state (updated 2026-09-22).** The table below is the original
> phase plan. Current P0 delivery against it:
>
> | P0 item | State |
> |---|---|
> | P4-P0-1 Official LongMemEval | Retrieval stage `COMPLETED` on all **500/500** cases (0 errors, 6,850 s = 1.90 h, formation coverage 739/896 = 82.48%); reader + official judge `NOT COMPLETED` |
> | P4-P0-2 Embedding comparison | 3-model run done; preregistered frozen-corpus protocol pending |
> | P4-P0-3 PostgreSQL 100K | `PARTIAL` — **not** "NOT STARTED" (corrected 2026-09-22). Main run `postgres-100k-20260921-084800` executed checkpoints A–F, stopped at G (`/dev/shm` exhaustion at 8 workers); mixed workload H never ran. 100K exact p50 156.12 / p95 165.93 ms. A concurrency recovery run (`...-110255`) shows branch A **PASS at 1/2/4/8 workers with 0/64 failures**, but that run is **unfinished** (`smoke`, `branch_b` 2/4/8, mixed workload, integrity audit, summary all `PENDING`) |
> | P4-P0-4 False Memory Test Suite | v2 gate run: **agreement 0.9706, 0 false accepts** (was 1); the `summary`-overwrites-user accept is closed by the new single-valued overwrite floor |
> | Formation gate root-cause + semantic fix | `COMPLETED` (6.14% → 84.82% offline micro-suite; 82.48% on the full corpus) |
> | 500-case sweep performance | `COMPLETED` — full run executed: **1.90 h measured**, 13.70 s/case, end-to-end **10.78×** (the earlier 13.47× / 1.52 h was a 4-case estimate, 25% optimistic) |
>
> Evidence: `docs/20-phase-4-results.md` (the consolidated P0 report),
> `docs/21-phase-4-architecture-review.md` (architecture verdicts),
> `docs/22-phase-4-production-gap-analysis.md` (readiness gaps),
> `docs/23-phase-4-readiness-checklist.md` (70 item-level verdicts),
> `docs/18-phase-4-evidence-gap-analysis.md` §5,
> `docs/benchmark/longmemeval-500case-profiling.md`,
> `docs/benchmark/longmemeval-report.md`, `docs/19-phase-4-experiment-plan.md`.
>
> **Phase 4 is closed as an evaluation milestone and the system remains
> non-production.** Readiness verdict `NOT READY`; see `docs/22` §5 for the
> statements the evidence does not support.

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
