# Phase 4 Evidence Gap Analysis

Audit date: 2026-09-20  
Baseline commit: `16f213464aab60c3e14563ede4109087a14fcaf4` (dirty Phase 3 worktree)  
Audit scope: Phase 3 documents, ADR-007–012, tests, raw evaluation artifacts, PostgreSQL implementation and the measured 10K run

> **Status update (2026-09-21).** The table below is the original Phase 3 audit
> and is preserved as the baseline it was written against. Several rows have
> since moved. The authoritative current wording lives in the row-level
> "Status update" notes in §5; the summary is:
>
> | Row | Was | Now | Evidence |
> |---|---|---|---|
> | Official LongMemEval performance | `NOT TESTED` | **`PARTIALLY VERIFIED`** — retrieval stage COMPLETED on all 500 official cases; QA/judge still `NOT COMPLETED` | `docs/benchmark/longmemeval-report.md` |
> | False memory is controlled | `NOT TESTED` | **`PARTIALLY VERIFIED`** — v2 gate: agreement **0.9706**, attack-refusal 0.4706, **0 false accepts** | `eval/reports/false-memory-v2-overwrite-floor.json` |
> | Current architecture better than naive vector memory | `NOT TESTED` | still **`NOT TESTED`** — no naive baseline was run | — |
> | BGE-M3 is the best model | `NOT TESTED` | **`PARTIALLY VERIFIED`** — 3-model comparison run, but the frozen project corpus of P4-P0-2 was not used | `eval/reports/embedding-comparison-latest.json` |
> | Write gate decision quality | `PARTIALLY VERIFIED` | **`PARTIALLY VERIFIED`** (improved) — semantic v2 gate raises gold-turn formation offline from 6.14% to ~84.82% | `docs/fix-write-gate-semantic-2026-09-21.md` |
>
> Two new documents carry the newer evidence:
> `docs/benchmark/longmemeval-500case-profiling.md` (performance, GPU
> feasibility) and `docs/diagnosis-formation-gate-2026-09-21.md` (formation
> root cause). Do not cite a row above as current without checking the update
> note.
>
> **Update 2026-09-22**: the full 500-case `LongMemEval_S` semantic-gate run has
> now **completed** (500/500 rows, 0 errors, 6,850 s = 1.90 h). This changes the
> scope of the first row in §5 — retrieval-stage figures now come from the whole
> corpus rather than a leading slice — while leaving the reader/judge gap
> unchanged. See §5.

## 1. Claim-status vocabulary

These labels describe the state of a **claim**, not whether code with a similar
name exists.

- **VERIFIED**: the stated claim was directly observed under a recorded,
  reproducible boundary and the evidence is strong enough for that exact scope.
- **PARTIALLY VERIFIED**: part of the claim was observed, or it was observed only
  under a narrower workload/environment than the claim requires.
- **DESIGN ONLY**: an architecture, policy or interface is documented but its
  runtime effect has not been demonstrated.
- **NOT TESTED**: an executable experiment is known but has not been run.
- **UNKNOWN**: the repository does not yet contain enough information to define
  or test the claim without additional research, data, policy or access.

`Implemented` is never a synonym for `VERIFIED`. A deterministic unit test can
verify a local invariant while leaving semantic quality, scale, operations and
external validity unverified.

## 2. Project evidence hierarchy

| Level | Name | Minimum evidence | What it can support |
|---:|---|---|---|
| E0 | Assumption | Rationale or design text only | A hypothesis to test; no runtime claim |
| E1 | Deterministic component evidence | Unit/property tests with fixed fixtures | Pure validation, transition or metric invariants |
| E2 | Controlled pipeline evidence | Multi-component synthetic integration with raw per-case output | Wiring and failure-path claims inside the synthetic boundary |
| E3 | Real-system integration evidence | Real database/model/service exercised locally, with versions and failure assertions | Correct operation of the measured integration path |
| E4 | Reproducible controlled benchmark | Frozen dataset/config, repeated samples, environment manifest and machine-readable results | Quality or performance for that dataset, workload, scale and environment |
| E5 | Official/external benchmark evidence | Official artifact and protocol, pinned source/version, disclosed adapter and judge | Comparison to that benchmark only; not production behavior |
| E6 | Representative scale/reliability evidence | Target-like distribution, concurrency, fault injection, cold/warm separation and resource capture | Capacity and failure claims within the tested envelope |
| E7 | Production observation | Defined SLOs, representative traffic, monitoring window, incidents and rollback evidence | Production reliability for the observed deployment window |

Evidence levels are not a single maturity ladder for the whole project. A
component can have E3 correctness evidence and still have only E0 scale
evidence. In particular:

- “PostgreSQL exact retrieval executes correctly” has E3 support.
- “The measured 10K exact-dense workload has p95 176.78 ms” has E4 support.
- “PostgreSQL exact search is sufficient at 1M” has E0 support and is NOT
  TESTED.

## 3. Evidence inventory

| Claim | Current Evidence | Evidence Type / Level | Confidence | Missing Evidence | Risk If Wrong | Required Experiment | Priority | Status |
|---|---|---|---|---|---|---|---|---|
| Strict candidate schema rejects malformed, unbounded and source-unclosed candidates | Deterministic candidate/provider tests | E1 | High for covered invariants | Real model output distribution and rejection analysis | Invalid or unsupported facts enter memory | Model-backed formation benchmark with raw provider outputs | P0 | VERIFIED |
| Write gate records features, thresholds, reasons and versions | Unit and service tests; stored decision schema | E1–E2 | High for persistence; low for decision quality | Calibrated labeled formation set | Explainable but wrong WRITE/SKIP decisions | False-memory suite plus held-out formation labels | P0 | PARTIALLY VERIFIED |
| Agent/tool/external statements cannot silently become user FACTs | Schema and provenance code/tests cover selected cases | E1–E2 | Medium | Adversarial end-to-end poisoning matrix | Durable false user facts and prompt-injection persistence | Provenance-enforcement and poisoning benchmark | P0 | PARTIALLY VERIFIED |
| Eight relationship classes are represented and mapped to deterministic actions | Resolution matrix and service tests | E1–E2 | High for authored cases | Larger ambiguous/model-generated corpus, per-class metrics | Incorrect merge, supersede or unresolved truth | False-memory benchmark and long-horizon simulation | P0 | VERIFIED |
| Temporal updates preserve current and historical truth | Chengdu→Shanghai and boundary tests | E1–E2 | Medium-high for rule fixtures | Natural-language temporal diversity and official benchmark slice | Stale current facts or rewritten history | Temporal slice in embeddings/LongMemEval and long-horizon run | P0 | PARTIALLY VERIFIED |
| Embedding generations stage, verify, cut over and roll back safely | Lifecycle tests including degraded envelopes | E1–E2 | High for local lifecycle | Real model backfill, interruption and resource use | Mixed/incomplete generations or silent fallback | Real-model lifecycle smoke and corruption/fault tests | P0 | PARTIALLY VERIFIED |
| BGE-M3 is the best model for this workload | Literature-informed ADR; no local model comparison | E0 | Low | Same-corpus BGE/E5/GTE/baseline quality and cost results | Wrong quality/latency/storage choice | Preregistered embedding comparison | P0 | NOT TESTED |
| Retrieval channels are independently traceable and configurable | Channel/trace tests and six-query A–H artifact | E1–E2 | High for tracing; low for quality contribution | Larger held-out semantic set and model-backed vectors | Complexity without measurable value | Embedding benchmark and Retrieval Ablation V2 | P0/P1 | PARTIALLY VERIFIED |
| Current full retrieval architecture is better than naive vector memory | Six-query synthetic run; dense is a non-semantic hash fixture | E2 | Very low | Semantic dense baseline, QA outcomes, paired uncertainty | Core architecture may not improve user answers | Official benchmark plus internal held-out ablation | P0 | NOT TESTED |
| Context packing deduplicates, filters time, groups contradictions and respects injected token counts | Deterministic packing tests | E1–E2 | High for invariants | Reader-tokenizer and answer-quality comparison | Token waste or contradictory answers in deployment | Packing ablation with exact tokenizer and QA metrics | P1 | PARTIALLY VERIFIED |
| PostgreSQL migrations and measured online path work | 8/8 real PG tests: migrations, locking, retry, rollback, isolation, purge and pgvector | E3 | High for covered path | Connection loss, restore, pool behavior and full contract parity | Operational inconsistency or unrecoverable jobs | Fault injection and expanded contract suite | P1 | VERIFIED |
| PostgreSQL and SQLite have management parity | Method audit shows replay/reindex/jobs/event deletion/export and lifecycle gaps | E0–E1 | High that parity is absent | Implemented and shared contract tests | Production backend cannot perform documented operations | Feature-matrix audit followed by parity implementation/tests | P1 | DESIGN ONLY |
| Application namespace isolation prevents cross-namespace access | Unit tests plus PG integration checks with explicit predicates | E2–E3 | Medium-high for measured calls | Database-layer RLS and worker/admin identity matrix | A missing WHERE clause leaks or deletes another tenant | PostgreSQL RLS migration and direct-SQL integration suite | P1 | PARTIALLY VERIFIED |
| PostgreSQL RLS enforces tenant isolation | No RLS migration or RLS integration evidence | E0 | None | All user/worker/admin read/write/search/delete cases | Database-level tenant breach | RLS experiment including missing/wrong identity | P1 | NOT TESTED |
| Hard purge removes live relational/vector/provenance residue in the measured PG schema | Real integration residue assertions | E3 | High inside live DB boundary | Cache/object store/export/backup/WAL/PITR semantics | Deleted data can reappear or survive elsewhere | Deletion propagation audit and restore experiment | P1 | PARTIALLY VERIFIED |
| Backup/restore honors erasure requirements | No backup, WAL or restore experiment; policy not fixed | E0 | None | Retention, expiry, restore-time replay and legal-hold decisions | Deleted personal data reappears after restore | Backup-erasure model plus physical/logical restore drill | P1 | UNKNOWN |
| 10K exact pgvector path meets provisional 500 ms p95 in one local workload | 20-query controlled run: p95 176.78 ms; 38.62 ingest/s | E4 | Medium for exact recorded setup | More samples, cold/warm, concurrency, real dimensions and filters | Misleading capacity planning | Expanded 10K/100K scale harness | P0/P1 | VERIFIED |
| PostgreSQL exact search remains acceptable at 100K | No 100K run | E0 | None | Real workload and resource measurements | Latency or ingest collapse | Real PostgreSQL 100K benchmark | P0 | NOT TESTED |
| PostgreSQL/pgvector remains appropriate at 1M | No 1M run | E0 | None | Stable 100K first; exact and ANN measurements | Wrong storage/search architecture | Gated 1M benchmark | P2 | NOT TESTED |
| HNSW or IVFFlat improves latency within an acceptable recall budget | No ANN index or exact-ground-truth comparison | E0 | None | Recall@K, filtered recall, build/update/storage/cold-warm data | Premature ANN adds cost or loses evidence | Exact vs HNSW vs IVFFlat benchmark | P1 | NOT TESTED |
| A dedicated vector database is required | No PostgreSQL threshold has been crossed | E0 | None | Measured trigger: latency, recall, build, memory, filter or operations | Unnecessary dual-write/deletion complexity | Architecture reconsideration gate after scale results | P2 | NOT TESTED |
| Official LongMemEval performance is known | Strict adapter has synthetic tests; no official artifact run | E1 only for adapter | None for benchmark score | Official repo/dataset/protocol, model/judge and completed run | Unsupported external-quality claims | Official run or explicitly labelled compatible subset | P0 | NOT TESTED |
| False memory is controlled | A tiny wiring smoke reported zero false memories | E2, n too small | Very low | Dedicated adversarial categories and denominators | High recall can conceal durable corruption | False Memory Test Suite | P0 | NOT TESTED |
| Long-term growth does not pollute retrieval | No controlled 100→1M quality curve | E0 | None | Fixed query set at increasing corpus sizes | Precision/recall and context degrade silently | Memory-pollution curve | P1 | NOT TESTED |
| Consolidation improves net quality | No consolidation ablation | E0 | None | No/simple/session/periodic/rule+LLM comparison | Summary drift and information loss | Consolidation ablation with false-memory guardrails | P1 | NOT TESTED |
| Reliability and integrity are production-ready | Unit/integration checks exist; no SLO/DR/observability evidence | E1–E3 fragments | Low for production | Representative failures, alerts, runbooks, capacity and production observation | Silent corruption or prolonged outage | Integrity checks, fault suite and readiness review | P2 | PARTIALLY VERIFIED |
| Production-ready | Explicitly prohibited and unsupported | Below E7 | None | RLS, restore, scale, SLO, deployment, DR, cost and observations | Material security/reliability harm | Complete readiness checklist; retain non-ready status until evidence exists | P2 | NOT TESTED |

## 4. Audit conclusions

1. The strongest Phase 3 claims concern deterministic invariants and the narrow
   real PostgreSQL integration path. Those claims should remain narrowly worded.
2. The A–H artifact is useful negative evidence but has only six queries and no
   semantic embedding. It cannot select the retrieval architecture.
3. The 10K PostgreSQL result is a real E4 system benchmark with synthetic 96-d
   vectors. It is neither semantic-model evidence nor evidence for 100K/1M.
4. Security evidence stops at application predicates and live-database purge.
   RLS and backup/PITR erasure are open.
5. Phase 4 must first close external quality, model choice, 100K scale and false
   memory evidence; ANN and production decisions are gated by those results.

## 5. Row-level status updates (2026-09-21, extended 2026-09-22)

These supersede the corresponding rows in §3. Each states what changed and what
is still missing; none of them upgrades a claim beyond its measured scope.

| Claim | Updated status | What is now measured | What is still missing |
|---|---|---|---|
| Official LongMemEval performance is known | `PARTIALLY VERIFIED` | All 500 official `LongMemEval_S` cases ran the formation+retrieval stage with 0 errors and 0 timeouts under a frozen configuration; gold evidence-turn formation coverage **739/896 = 82.48%**; memory-level ranking (turn Recall-any@10 **0.632**, session Recall-any@10 **0.777**, MRR 0.452/0.641) | Reader generation and the official GPT-4o judge — no answer-quality or official overall score exists. The retrieval block is a memory-level summary, **not** the official turn/session baseline |
| False memory is controlled | `PARTIALLY VERIFIED` | Semantic v2 gate + single-valued overwrite floor: oracle agreement **0.9706**, attack-refusal rate 0.4706, user-fact accuracy **1.0000**, **0 false accepts** (down from 12 under v1). The `sd-summary-overwrites-user` accept is closed by the floor | One residual `false_reject` (`ec-third-party-residence`) remains, and it is **not repairable at the gate**: the harness scores the single boolean `decision.accepted` against two *opposite* expectations (`memory_expected=true` on line 110, `user_fact_allowed=false` on line 117), so no accept/reject policy satisfies both — a three-valued outcome is needed. Proximate trigger is the sentence-level negation guard (`do not` → whole sentence refused). The obvious clause-scoped fix was **measured and rejected**: it only moves the error between axes (memory_expected 33→34, user_fact 34→33, net zero) and would re-admit **18,162 of 246,738 corpus turns (7.36%)**, mostly assistant chatter, against a negation guard that refuses 14.39% of the corpus. Denominator is 34 events, so intervals are wide. See `fix-write-gate-semantic-2026-09-21.md` §4.4 |
| Current full retrieval architecture is better than naive vector memory | `NOT TESTED` | — | No naive-vector baseline has been run under the same queries; the dense channel is still a non-semantic hash fixture in the A–H artifact |
| BGE-M3 is the best model for this workload | `PARTIALLY VERIFIED` | A pinned 3-model comparison was executed (`bge-m3`, `multilingual-e5-large-instruct`, `gte-multilingual-base`) | The frozen P4-P0-2 project corpus (split by user/session, with contradiction and near-duplicate slices) was not used, so this is not the preregistered comparison |
| Write gate records features, thresholds, reasons and versions | `PARTIALLY VERIFIED` (improved) | The semantic v2 gate is calibrated on **22** labelled probes (an earlier revision of this row said 19 — stale) and raises gold evidence-turn formation from 6.14% offline → **84.82% offline (micro-suite)** / **82.48% (full 500-case corpus)**. A disjoint **29-probe held-out** decision set also exists and scores **0.8276**, Wilson 95% [0.6545, 0.9240] (vs 1.0000 in-sample) | The gain is measured at the gate (offline and full-corpus formation), not end-to-end answer quality. A held-out **evidence-turn formation** set still does not exist — the held-out set above is a *decision* set, which is a different object |
| Performance: the 500-case run is feasible in one sitting | **`VERIFIED`** (this machine) | **Full run executed 2026-09-22**: 500/500 rows, 0 errors, **6,850 s = 1.90 h**, **13.70 s/case**; end-to-end **10.78×** vs the 20.5 h CPU projection, against a 13.47× 4-case estimate | Single machine, single process; multi-process throughput only projected. Note the projection was 25% optimistic and per-case cost is not predictable from turn count (R² = 0.03) — see `longmemeval-500case-profiling.md` §10 |
| PostgreSQL exact search remains acceptable at 100K | **`PARTIALLY VERIFIED`** — §3 row 94 ("No 100K run", `NOT TESTED`) is **outdated** | A 100K run **was executed**: `postgres-100k-20260921-084800`, checkpoints **A–F PASS**, 100,000 memories / 100,000 96-d vectors. 100K exact retrieval **p50 156.12 ms / p95 165.93 ms** (inside the provisional 500 ms target); filtered p50 20.01/30.87/81.51 ms at 1/10/50%; temporal current p50 301.25 ms; COPY 20,279 rows/s; service ingest 57.49/s (1 writer) and 177.31/s (4 writers) | The run is **`PARTIAL` — it stopped at Checkpoint G**; mixed workload never ran. The 100K corpus was **bulk-loaded without the full event/provenance projections**, so it is **not directly comparable** to the 10K service baseline and no linear-scaling claim is supported |
| PostgreSQL holds at 100K with 8 concurrent workers | **`PARTIALLY VERIFIED`** — new evidence, run **incomplete** | Recovery run `postgres-100k-concurrency-20260921-110255`, **branch A**: workers 1/2/4/8 all `PASS`, **0 failed operations** out of 8/16/32/64, `read_consistent = true`, 0 unexpected row-count delta. Throughput 18.1 → 34.6 → 48.6 → **62.1 ops/s**; retrieval p50 49.6 → 51.4 → 71.7 → **87.1 ms** (8-worker p95 239 ms). The earlier `44/64` failure is **not reproduced** with `max_parallel_workers_per_gather = 2` — the `/dev/shm` exhaustion was configuration-induced | The recovery run is **itself unfinished**: `smoke`, `branch_b` (workers 2/4/8), `mixed_workload`, `integrity_audit` and `summary` are all still `PENDING`; manifest status `PENDING`, last touched 2026-09-21T03:34:55Z. Branch B (`max_parallel_workers_per_gather = 0`, session-only) has only worker-1. **No final summary artifact exists for the recovery run** |

The formation root cause and the retrieval-stage numbers are documented in
`docs/diagnosis-formation-gate-2026-09-21.md` and
`docs/benchmark/longmemeval-report.md` respectively.

The Phase 4 closure set reads this matrix and does not restate it:
`docs/20-phase-4-results.md` (results), `docs/21-phase-4-architecture-review.md`
(architecture verdicts per question), `docs/22-phase-4-production-gap-analysis.md`
(18 readiness domains) and `docs/23-phase-4-readiness-checklist.md` (70
item-level verdicts). Where a row above and one of those documents disagree, they
are newer; where either disagrees with `eval/reports/**`, the artifact wins.


