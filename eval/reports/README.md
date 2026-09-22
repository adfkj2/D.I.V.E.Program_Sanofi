# Evaluation reports

Committed reports must include a `dive-eval-manifest-v1` manifest or link to the
external immutable artifact that contains it. A report must state backend,
dataset/version/hash, model/revision, retrieval configuration, hardware and
whether it evaluates formation, evolution, retrieval, QA, or system behavior.

`p0-offline-latest.json` is a six-case deterministic regression and exercises
the A–H harness. It is not evidence that the hash embedder has semantic quality
and is not a LongMemEval result.

## LongMemEval artifacts

| Artifact | What it is | What it is **not** |
|---|---|---|
| `longmemeval-s-retrieval-latest.json` | Full 500-case formation+retrieval stage, `utility-baseline-v1` write gate, commit `16f2134` | Not a QA score; no reader, no official judge |
| `longmemeval-s-retrieval-gpu.json` | Full 500-case stage, `semantic-utility-v2.1` gate, `device=cuda` (RTX 4060). **500/500 rows, 0 errors, 6,850 s = 1.90 h, 13.70 s/case; formation coverage 739/896 = 82.48%** | Not a QA score; GPU does not change gate decisions (max score delta 1.08e-7). The `retrieval` block is a memory-level ranking summary, **not** the official turn/session baseline |
| `longmemeval-profile-v1.json` | Stage-split profile, v1 gate, 5 cases | Not a benchmark result; 5 cases |
| `longmemeval-profile-semantic.json` | Stage-split profile, semantic gate, 3 cases, GPU | Not a benchmark result. **Its `per_case_mean_ms` stages overlap and must not be summed** — see profiling doc §3a |

Stage status for every LongMemEval artifact: retrieval `COMPLETED`, reader
answer generation `NOT COMPLETED`, official GPT-4o judge `NOT COMPLETED`, and
semantic abstention `NOT_EVALUATED`. Never present these as an official
LongMemEval score, and never quote a per-stage *share* of the wall clock.

Methodology: [`../../docs/benchmark/longmemeval-methodology.md`](../../docs/benchmark/longmemeval-methodology.md).
Results: [`../../docs/benchmark/longmemeval-report.md`](../../docs/benchmark/longmemeval-report.md).
Performance/GPU: [`../../docs/benchmark/longmemeval-500case-profiling.md`](../../docs/benchmark/longmemeval-500case-profiling.md).

## False-memory artifacts

| Artifact | Gate | Key figures |
|---|---|---|
| `false-memory-v1.json` | `utility-baseline-v1` | agreement 0.6471, attack-refusal 0.0882, 12 false accepts |
| `false-memory-v1-rerun.json` | `utility-baseline-v1` (re-run) | same policy, re-executed |
| `false-memory-v2.json` | `semantic-utility-v2.1` | agreement 0.9412, attack-refusal 0.4412, 1 false accept |
| `false-memory-v2-rerun.json` | `semantic-utility-v2.1` (re-run) | user-fact accuracy 0.9706; residual accept is `sd-summary-overwrites-user` via `summary` |
| `false-memory-v2-overwrite-floor.json` | `semantic-utility-v2.1` + single-valued overwrite floor | **agreement 0.9706, attack-refusal 0.4706, user-fact accuracy 1.0000, 0 false accepts** — the `sd-summary-overwrites-user` accept is closed |

The two earlier v2 artifacts are the **pre-fix** record and are retained on
purpose: the fix is linked to the failure it closes rather than replacing it.

Denominator is 34 events, so confidence intervals are wide. The oracle is
evaluated at the gate, not through the full service.

> The remaining `false_reject` in all v2 artifacts is
> `ec-third-party-residence` ("Alice lives in Shanghai; do not treat this as my
> residence."). It is a **pre-existing and separate** issue, not a regression
> from the overwrite floor: the harness compares `decision.accepted` against
> `oracle.memory_expected`, while the gate's acceptance means "may become a
> *user* fact". That event is `memory_expected=true` but
> `user_fact_allowed=false`, so the two axes disagree by construction — no
> accept/reject policy can satisfy both, and resolving it requires deciding
> whether the gate should be able to express "form an entity memory but not a
> user fact", which is an open design question.
>
> The obvious local fix — making the negation guard clause-scoped instead of
> sentence-scoped — was **measured and rejected** (2026-09-22). It does not help
> (`memory_expected` 33→34, `user_fact` 34→33: the error merely moves between
> axes) and it would re-admit **18,162 of 246,738 LongMemEval turns (7.36%)**,
> mostly assistant chatter with incidental negations. Full record:
> [`../../docs/fix-write-gate-semantic-2026-09-21.md`](../../docs/fix-write-gate-semantic-2026-09-21.md) §4.4.

## PostgreSQL 100K artifacts

| Artifact | What it is | What it is **not** |
|---|---|---|
| `postgres-100k/postgres-100k-20260921-084800/` | 100K-row pgvector infrastructure run: checkpoints **A–F PASS**, final integrity 100,000 memories / 100,000 96-d vectors. Exact p50 156.12 / p95 165.93 ms | **`PARTIAL`** — stopped at checkpoint **G** (`/dev/shm` exhaustion at 8 workers, 44/64 ops failed); mixed workload H never ran. Corpus was bulk-loaded **without** full event/provenance projections, so it is not comparable to the 10K service baseline. Synthetic 96-d vectors — no semantic-quality claim |
| `postgres-100k-concurrency/postgres-100k-concurrency-20260921-110255/` | Concurrency recovery, branch A: workers 1/2/4/8 all `PASS`, **0 failed ops** (8/16/32/64), 18.1→62.1 ops/s, retrieval p50 49.6→87.1 ms | **Unfinished**: `smoke`, `branch_b` (2/4/8), `mixed_workload`, `integrity_audit` and `summary` are all `PENDING`; the manifest status is `PENDING` and **no final summary artifact exists**. Do not cite this as a completed recovery |

Reports: [`../../docs/benchmark/postgres-exact-100k-report.md`](../../docs/benchmark/postgres-exact-100k-report.md),
[`../../docs/benchmark/postgres-exact-10k-report.md`](../../docs/benchmark/postgres-exact-10k-report.md),
[`../../docs/benchmark/postgres-100k-concurrency-rerun-plan.md`](../../docs/benchmark/postgres-100k-concurrency-rerun-plan.md).
