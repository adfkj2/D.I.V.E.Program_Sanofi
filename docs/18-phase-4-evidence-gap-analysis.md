# Phase 4 Evidence Gap Analysis

Audit date: 2026-09-20  
Baseline commit: `16f213464aab60c3e14563ede4109087a14fcaf4` (dirty Phase 3 worktree)  
Audit scope: Phase 3 documents, ADR-007–012, tests, raw evaluation artifacts, PostgreSQL implementation and the measured 10K run

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

