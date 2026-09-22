# Phase 4 Preregistered Experiment Plan

Plan date: 2026-09-20  
Baseline commit: `16f213464aab60c3e14563ede4109087a14fcaf4` plus the preserved dirty Phase 3 worktree  
Principle: **Evidence > Feature Count**

> ## Execution log (2026-09-21)
>
> Status of each preregistered experiment, with the artifact that carries it.
> A row is only `DONE` if the *whole* experiment in this plan ran; partial runs
> keep the label of the stage that actually completed.
>
> | Experiment | Status | Artifact / note |
> |---|---|---|
> | P4-P0-1 Official LongMemEval | Retrieval stage `DONE` on all 500 cases; reader + official judge `NOT_COMPLETED` | `eval/reports/longmemeval-s-retrieval-gpu.json` (semantic v2 gate, `device=cuda`, **500/500 rows, 0 errors, 6,850 s = 1.90 h**); `eval/reports/longmemeval-s-retrieval-latest.json` (v1 gate); `docs/benchmark/longmemeval-report.md` |
> | P4-P0-2 Embedding comparison | `PARTIAL` | 3 models pinned and compared regionally; the frozen project corpus (split by user/session, contradiction and near-duplicate slices) was **not** used, so the preregistered protocol is unmet |
> | P4-P0-3 PostgreSQL 100K | **`PARTIAL`** (corrected 2026-09-22 — an earlier revision of this log said `NOT STARTED`, which was wrong) | Main run `postgres-100k-20260921-084800`: checkpoints A–F `PASS`, **G `FAILED`** (`/dev/shm` exhausted at 8 workers, 44/64 ops), **H `NOT_COMPLETED`**. See `docs/benchmark/postgres-exact-100k-report.md`. Recovery run `postgres-100k-concurrency-20260921-110255`: branch A `PASS` at 1/2/4/8 workers, **0 failed ops**, 62.1 ops/s and p50 87.1 ms at 8 workers — but the run itself is **unfinished** (`smoke`, `branch_b` 2/4/8, mixed workload, integrity audit and summary are all `PENDING`; no final summary artifact) |
> | P4-P0-4 False Memory Test Suite | `DONE` (suite exists, both policies run; v2 false accepts closed) | `false-memory-v1.json`, `false-memory-v2-rerun.json`, **`false-memory-v2-overwrite-floor.json`**; v2 now **agreement 0.9706, 0 false accepts, user-fact accuracy 1.0000** (was 0.9412 / 1 / 0.9706) |
> | GPU feasibility + full 500-case run (enabling work, not a preregistered experiment) | `DONE` | **Full run completed 2026-09-22**: 500/500 rows, 6,850 s = **1.90 h**, **13.70 s/case**, end-to-end **10.78×** vs the CPU projection, formation coverage **739/896 = 82.48%**. Recorded in `docs/benchmark/longmemeval-500case-profiling.md` §0/§10 |
>
> Two defects found by these runs were fixed and pinned with tests:
> `PrototypeIndex.score_many` lost its own results to cache eviction (it crashed
> the 500-case sweep), and the reader-side wiring gap. Both are linked from
> `docs/18-phase-4-evidence-gap-analysis.md` §5. No benchmark-specific answer,
> query or mutation behaviour was added.
>
> Two further findings are recorded but **not** fixed, because they are
> instrumentation/reporting issues rather than correctness defects:
>
> 1. `ops/longmemeval_profile.py` records per-stage timers that **overlap**, so
>    `per_case_mean_ms.warm` and `.ingest` cannot be summed
>    (`longmemeval-500case-profiling.md` §3a). Do not derive per-stage shares
>    from them.
> 2. The pre-run projection of 1.52 h was **25% too optimistic**; the measured
>    figure is 1.90 h, and per-case latency is **not** predictable from turn
>    count (R² = 0.03). The leading (unconfirmed) explanation is FIFO cache
>    saturation late in the corpus — see §10 for the falsification test.
>
> **What P4-P0-1 still does not have:** any QA/quality number. Reader is
> `NOT_EVALUATED`, QA generation and the official judge are `NOT_COMPLETED`,
> abstention is `NOT_EVALUATED`. The retrieval block is a memory-level ranking
> summary, not the official turn/session baseline.
>
> **Consolidated report:** `docs/20-phase-4-results.md` collects all P0 results
> with their artifact-contract compliance, Wilson intervals for the false-memory
> rates, the full failure/correction record, and the explicit list of what Phase 4
> does not establish. Where this log and that report disagree, the report is
> newer; where either disagrees with `eval/reports/**`, the artifact wins.
>
> **Closure status (2026-09-22).** All four documents §6 requires are published:
> `docs/20-phase-4-results.md` (results),
> `docs/21-phase-4-architecture-review.md` (14 architecture questions answered
> from executed evidence),
> `docs/22-phase-4-production-gap-analysis.md` (18 readiness domains),
> `docs/23-phase-4-readiness-checklist.md` (70 item-level verdicts).
> The checklist's overall verdict is **`NOT READY`** — Phase 4 completion is a
> research/evaluation milestone, not a production readiness statement
> (this plan's §P4-P2-3). **Phase 4 stops here; Phase 5 does not start
> automatically.**

## 1. Execution rules

Experiments execute in the order below. Only one resource-heavy benchmark runs
at a time. A later stage may begin only when its gate is satisfied or the
earlier stage has a recorded `NOT COMPLETED` result and the later stage does not
depend on it.

1. P0-1 official LongMemEval investigation/integration.
2. P0-2 real embedding comparison.
3. P0-3 real PostgreSQL 100K benchmark.
4. P0-4 false-memory benchmark.
5. P1 ANN, RLS, deletion/backup, pollution and consolidation experiments.
6. P2 1M, cost/readiness and vector-database reconsideration.

The core system may be fixed for a correctness defect discovered by an
experiment, but the failed artifact is retained and the fix is linked. No
benchmark-specific hard-coded answer, query or mutation behavior is allowed.

## 2. Common artifact contract

Every executable run writes a JSON artifact containing at least:

```text
schema_version, run_id, status, started_at, finished_at
git_commit, git_dirty, command, config, config_sha256
dataset {name, version, source, license, sha256, sample_count}
environment {os, hardware, python, docker, postgres, pgvector}
models[] {provider, model, revision, dimension, dtype, device}
seed, warmup_count, measured_sample_count
metrics, per_case_or_sample_rows, errors, timeouts
resource_measurements, limitations, raw_artifact_paths
```

`status` is one of `COMPLETED`, `PARTIAL`, `NOT_COMPLETED`, or `FAILED`.
Secrets and full DSNs are never recorded. Reports must link to the raw artifact
and reproduce its scope and limitations.

Latency reports include warm-up, sample count, median/p95/p99 and, where useful,
mean/std. Quality reports include denominators and per-category results. Any
small sample is labelled high uncertainty. Exact top-k is ANN ground truth.

## 3. P0 experiments

### P4-P0-1 — Official LongMemEval

**Question.** What does the official benchmark say about this system's
retrieval and final-answer behavior under the official protocol?

**Pre-run freeze.** Record official repository commit, dataset filename/hash,
release/version/license, task definitions, prompts, judge/evaluation model,
token settings, retrieval configuration and adapter version. Gold answers,
question types, `answer_session_ids` and `has_answer` remain outside the writer
allow-list.

**Treatments.** Run the official full protocol if dataset, evaluation model and
budget are available. Otherwise run the largest protocol-compatible subset and
label it `official-compatible subset`; never call it a full score.

**Metrics.** Official overall score and available official categories;
separately report evidence Precision/Recall@1/5/10, MRR, nDCG, abstention,
errors, timeouts and unsupported cases. Retrieval and QA are separate stages.

**Stop conditions.** Stop and record `NOT_COMPLETED` if the official dataset
cannot legally be accessed, required model credentials/budget are absent, the
official evaluator cannot run in this environment, or adapter validation finds
schema ambiguity that cannot be resolved from official sources. Synthetic data
must not replace the missing run.

**Artifacts.** `docs/benchmark/longmemeval-methodology.md`,
`docs/benchmark/longmemeval-report.md`, and
`eval/reports/longmemeval-<run-id>.json`.

### P4-P0-2 — Embedding model comparison

**Question.** Which real embedding model best serves the bilingual long-term
memory workload under a common retrieval protocol?

**Frozen corpus.** A versioned project dataset containing semantic paraphrase,
preference, old episodic recall, temporal change, conflicting facts,
near-duplicates, entity ambiguity and long-tail facts. Split by user/session;
the test partition is untouched during configuration selection.

**Treatments.** Existing lexical/non-semantic baseline, BGE-M3 dense,
`multilingual-e5-large-instruct`, and `gte-multilingual-base`, all pinned to an
exact revision. Same corpus, queries, filters, top-k and metric code. Required
query/document prefixes are recorded as model protocol, not hidden tuning.

**Metrics.** Recall@1/5/10, MRR, nDCG@10, Hit Rate, temporal and contradiction
retrieval accuracy, per-slice results, embedding/query latency and throughput,
load time, dimension, serialized storage, process CPU RAM and GPU peak memory
when applicable.

**Decision rule.** No model is “best” without a declared priority. Reject a
candidate if it violates provenance/temporal hard filters or cannot complete
the frozen set. Recommend from the Pareto frontier of quality, latency and
memory. Report ties/uncertainty rather than using leaderboard reputation.

**Artifacts.** Frozen dataset/config, per-query JSONL,
`eval/reports/embedding-comparison-<run-id>.json`,
`docs/benchmark/embedding-model-comparison.md`, and an ADR-009 update.

### P4-P0-3 — PostgreSQL 100K scale

**Question.** What are real 100K PostgreSQL + pgvector exact-path quality,
latency, throughput and resource boundaries?

**Workload.** 100,000 memories with recorded vector model/dimension and tenant
distribution. Measure bulk and incremental ingest, random/exact semantic read,
filtered and temporal read, soft delete, hard purge, concurrent ingest,
concurrent retrieval and a mixed workload. Record query plans and cache state.

**Protocol.** Separate cold and warm queries; warm up before measured warm
iterations. Use at least 100 measured retrieval samples per reported treatment
unless runtime/resource failure is recorded. Capture host/container CPU, RAM,
database/table/index/embedding size, buffer/cache observations and concurrency.

**Stop conditions.** Abort safely if free disk/RAM crosses a preregistered
guardrail, a correctness invariant fails, or projected remaining runtime makes
the run unsafe. Keep the partial artifact; do not extrapolate it to 100K.

**Artifacts.** Scale runner/config, raw samples and plans,
`eval/reports/postgres-100k-<run-id>.json`, failure record if applicable, and
`docs/benchmark/postgres-100k-report.md`.

### P4-P0-4 — False Memory Test Suite

**Question.** Does formation/evolution create or promote unsupported memory?

**Frozen categories.** Extraction Hallucination, Inference Promotion,
Incorrect Merge, Incorrect Supersede, Entity Confusion, Temporal Confusion,
Generated Response Feedback and Summary Distortion. Include prompt injection,
untrusted web/tool results, agent statements, user corrections and malicious
external content with explicit provenance labels.

**Metrics.** False Memory Creation Rate, False Fact Promotion Rate, Incorrect
Supersede Rate, Unsupported Inference Rate and Provenance Loss Rate, each with
numerator, denominator, per-category rows and Wilson confidence intervals.

**Hard guardrail.** An agent-generated or external statement without user
evidence must not become a USER FACT. Any violation blocks a safety claim even
if aggregate recall improves.

**Artifacts.** Versioned JSON dataset, runner, per-case JSONL,
`eval/reports/false-memory-<run-id>.json`, and
`docs/benchmark/false-memory-report.md`.

## 4. P1 experiments

### P4-P1-1 — Exact vs HNSW vs IVFFlat

Run only after the 100K exact ground truth is stable. Compare exact, HNSW and
IVFFlat at 10K/100K; include 1M only in P2. Tune `ef_search` and
`lists/probes` on dev queries only. Report Recall@K against exact, latency,
filtered recall at different selectivities, build time, update/delete behavior,
storage/RAM and cold/warm results. ANN is adopted only if it produces a
material latency gain while meeting a preregistered recall floor; otherwise
exact remains selected.

### P4-P1-2 — PostgreSQL RLS

Create a database identity/tenant model and test direct query, retrieval,
insert/update/delete, batch paths, background/consolidation worker, wrong and
missing identity, and explicit administrator path. Deliberately issue
application SQL without tenant predicates to prove the database boundary.
Capture permitted/denied rows and RLS overhead. Application authorization and
RLS responsibilities remain distinct.

### P4-P1-3 — Deletion and backup boundary

Trace Source Event → Derived Memory → Embedding → Index → Relation → Cache →
Audit artifact for User Forget, Memory Delete, Source Delete, Account Delete and
GDPR-like Erasure. Then perform backup/restore experiments for the chosen
PostgreSQL backup/WAL mechanism. Test whether deleted rows reappear and whether
restore-time erasure replay removes them. Publish guarantees and exclusions in
`docs/security/backup-erasure-model.md`; do not promise physical erasure before
backup expiry unless demonstrated.

### P4-P1-4 — Memory-pollution curve

Use a fixed frozen query/gold set against corpus sizes 100, 1K, 10K, 100K and,
only after the P2 scale gate, 1M. Report recall, precision, latency, candidate
set size, packing coverage, duplicates, contradictory context and false-memory
rate. Never substitute a smaller run plus extrapolation.

### P4-P1-5 — Consolidation and packing ablations

Compare no consolidation, simple dedup, session, periodic and rule+LLM only
where actually implemented. Report storage reduction, retrieval/answer
quality, information loss, temporal correctness, false memory, summary drift
and cost. Separately compare raw top-k, diversity-aware, temporal-aware,
importance-aware and current packing using the same retrieved candidates and
exact reader tokenizer. A variant is promoted only on held-out net benefit.

## 5. P2 experiments and gates

### P4-P2-1 — 1M scale

Begin only after the 100K correctness and operational run is stable and local
disk/RAM/time guardrails are documented. Measure exact and the surviving ANN
candidate for latency, exact-relative recall, build time, storage, RAM and
operational complexity. A failed exact run is a result, not evidence to hide.

### P4-P2-2 — Parameterized cost model

Model 1K/10K/100K daily users with replaceable parameters for embedding calls,
LLM tokens, reranking, consolidation, GB-month storage and compute. Record
workload assumptions and sensitivity ranges. Do not embed unverified vendor
prices.

### P4-P2-3 — Production-readiness review

Assess data integrity, concurrency, security, RLS, backup/restore, migration,
observability, capacity, deployment/rollback, DR, erasure, secrets, logs,
metrics, alerts, cost, SLO and runbooks. Only direct evidence receives `PASS`;
all other items are `PARTIAL`, `NOT TESTED`, `NOT IMPLEMENTED` or
`NOT APPLICABLE`. Phase 4 completion does not imply production readiness.

### P4-P2-4 — Vector-database reconsideration gate

Evaluate Qdrant, Milvus, Weaviate or OpenSearch only if measured PostgreSQL
results violate a stated latency/recall, filter, index-build, memory, scaling or
operational requirement after reasonable PostgreSQL tuning. Without a crossed
threshold, the decision is “do not introduce another datastore,” not “vector
databases were proven inferior.”

## 6. Phase 4 completion criteria

Phase 4 closes when the final review can answer each requested architecture
question from executed evidence or explicitly records it as a remaining
unknown with the blocking condition. Completion requires the P0 artifacts,
evidence matrix updates, architecture/production reviews, raw outputs, failure
records and reproducible commands. It does not require a favorable result.

After publishing `docs/20-phase-4-results.md`,
`docs/21-phase-4-architecture-review.md`,
`docs/22-phase-4-production-gap-analysis.md` and the readiness checklist,
Phase 4 stops. Phase 5 does not start automatically.

