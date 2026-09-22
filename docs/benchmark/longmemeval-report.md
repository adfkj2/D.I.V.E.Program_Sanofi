# LongMemEval_S retrieval-stage report

Date: 2026-09-20

> **Scope note, added 2026-09-22.** This report documents the run made with the
> **default `utility-baseline-v1` keyword write gate**
> (`eval/reports/longmemeval-s-retrieval-latest.json`). Its headline negative
> result — only **55/896 = 6.14%** of gold evidence turns formed a memory — is
> **specific to that gate**, not a property of the current system.
>
> A later run with the calibrated **`semantic-utility-v2.1`** gate over the same
> 500 cases (GPU) raises gold evidence-turn formation to **739/896 = 82.48%**,
> with 0 case errors in 6,850 s. See
> [`longmemeval-500case-profiling.md`](longmemeval-500case-profiling.md) §0 and
> the artifact `eval/reports/longmemeval-s-retrieval-gpu.json`.
>
> Both runs remain **retrieval-stage only**: no answer was generated and the
> official GPT-4o judge was not invoked, so **neither carries an official
> LongMemEval score**. The formation-coverage difference is measured at the
> write gate, not at answer quality.

Overall artifact status: `PARTIAL`

Retrieval stage: `COMPLETED` — 500/500 cases

QA generation: `NOT COMPLETED`

Official GPT-4o judge: `NOT COMPLETED`

## Result boundary

This is a D.I.V.E formation-and-retrieval run over all 500 questions in the
official `LongMemEval_S` cleaned artifact. It is **not** an official full
LongMemEval QA score. No answer was generated and the official GPT-4o judge was
not invoked because reader/judge credentials were not configured. Metric
definitions and the leakage boundary are documented in
[`longmemeval-methodology.md`](longmemeval-methodology.md).

The completed retrieval stage is a useful negative result. The system formed a
memory for only 55 of 896 gold evidence turns (6.14%). Turn-level
Recall-any@10 was 8.09%, and abstention accuracy was 0/30. The much higher
session-level Recall-any@10 of 77.45% only shows that retrieval often reached
some memory from a gold session; it does not show that the exact evidence turn
was retained or that a correct answer could be produced.

## Provenance and run identity

| Item | Recorded value |
|---|---|
| Official repository commit | `9e0b455f4ef0e2ab8f2e582289761153549043fc` |
| Cleaned dataset repository commit | `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| Dataset | `LongMemEval_S-cleaned` |
| Dataset file SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Oracle file SHA-256 | `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c` (downloaded, not used) |
| D.I.V.E Git commit | `16f213464aab60c3e14563ede4109087a14fcaf4` (`main`, dirty) |
| Adapter | `longmemeval-adapter-v2` |
| Configuration SHA-256 | `c8361bd7259f7904546e2c34b47ec03d92b58bede5b77bfb0339c51b3c12c30b` |
| Run id | `longmemeval-retrieval-20260920T114755Z` |
| Runtime | CPython 3.14.0, Windows 11 |
| Hardware | CPU/RAM not recorded in this artifact |
| Seed | No runner seed parameter or artifact seed field |
| Raw artifact | `eval/reports/longmemeval-s-retrieval-latest.json` |
| Raw artifact SHA-256 | `b574c4a07bc901842186b91cdcabf4d7431611dada3de202d304ffa4fc0d8e4b` |

The dirty worktree is material: the recorded commit does not, by itself,
reconstruct the Phase 4 adapter and runner state. The raw artifact hash and the
reviewed source state are part of the evidence package.

## Execution accounting

| Measure | Result |
|---|---:|
| Dataset cases | 500 |
| Retrieval rows completed | 500 |
| Answerable cases scored for evidence retrieval | 470 |
| Abstention cases scored separately | 30 |
| Case errors | 0 |
| Timeouts | 0 |
| Degraded retrieval rows | 0 |
| Total run duration | 136.28 s |
| QA answers generated | 0 — `NOT COMPLETED` |
| Official judge evaluations | 0 — `NOT COMPLETED` |

All 500 rows record the same executed channel set: BM25, dense, predicate,
temporal, entity and relation. Multi-hop was excluded. The configured
deterministic vectors were excluded from semantic dense scoring.

## Formation evidence

| Measure | Count / rate |
|---|---:|
| Non-empty turns ingested | 246,738 |
| Memories formed | 57,758 |
| Memories per non-empty turn | 23.41% |
| Gold evidence turns | 896 |
| Gold evidence turns that formed at least one memory | 55 |
| Gold evidence-turn formation coverage | **6.14%** |

Formation is the dominant observed failure boundary. Once an exact evidence
turn fails to form any memory, downstream memory retrieval cannot recover that
turn. Session-level scoring can still succeed by retrieving a different memory
from the same session, which explains much of the gap between the two relevance
levels below.

## Overall retrieval results

All values are macro averages over the 470 answerable questions.

### Turn-level evidence

| k | Recall-any | Recall-all | Evidence coverage | Precision | nDCG |
|---:|---:|---:|---:|---:|---:|
| 1 | 4.04% | 2.55% | 3.26% | 4.04% | 4.04% |
| 5 | 7.23% | 3.62% | 5.17% | 1.45% | 4.72% |
| 10 | 8.09% | 3.83% | 5.67% | 0.81% | 4.90% |

Turn-level MRR: **5.57%**.

### Session-level evidence

| k | Recall-any | Recall-all | Evidence coverage | Precision | nDCG |
|---:|---:|---:|---:|---:|---:|
| 1 | 51.28% | 14.47% | 31.09% | 51.28% | 51.28% |
| 5 | 69.79% | 43.83% | 56.21% | 36.72% | 52.24% |
| 10 | 77.45% | 54.04% | 65.84% | 26.19% | 55.98% |

Session-level MRR: **60.33%**.

Session-level relevance is a coarse diagnostic, not a replacement for exact
turn evidence. A hit means that a returned memory has a source in a gold
session, even when it does not contain the labelled answer turn.

## Results by published question type

The `Total` column includes abstention rows carrying that published type. The
`Scored` column is the answerable-case count used by the retrieval metrics.

| Published type | Total | Scored |
|---|---:|---:|
| `knowledge-update` | 78 | 72 |
| `multi-session` | 133 | 121 |
| `single-session-assistant` | 56 | 56 |
| `single-session-preference` | 30 | 30 |
| `single-session-user` | 70 | 64 |
| `temporal-reasoning` | 133 | 127 |
| **Total** | **500** | **470** |

No separate `information-extraction` string exists in the artifact used by
this run. The report preserves the released labels rather than retroactively
mapping the single-session categories to a different name.

### Turn-level slice metrics

| Published type | Recall-any@1 | @5 | @10 | MRR | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| `knowledge-update` | 5.56% | 8.33% | 8.33% | 6.94% | 4.35% |
| `multi-session` | 0.83% | 4.13% | 6.61% | 2.56% | 2.00% |
| `single-session-assistant` | 16.07% | 23.21% | 23.21% | 19.35% | 20.34% |
| `single-session-preference` | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| `single-session-user` | 6.25% | 9.38% | 9.38% | 7.81% | 7.24% |
| `temporal-reasoning` | 0.79% | 3.15% | 3.94% | 1.78% | 1.14% |

### Session-level slice metrics

| Published type | Recall-any@1 | @5 | @10 | MRR | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| `knowledge-update` | 70.83% | 87.50% | 91.67% | 79.01% | 71.07% |
| `multi-session` | 64.46% | 87.60% | 94.21% | 75.48% | 64.04% |
| `single-session-assistant` | 37.50% | 58.93% | 62.50% | 47.67% | 51.34% |
| `single-session-preference` | 16.67% | 46.67% | 63.33% | 31.54% | 39.10% |
| `single-session-user` | 53.13% | 73.44% | 85.94% | 63.97% | 69.21% |
| `temporal-reasoning` | 40.94% | 51.18% | 59.06% | 45.86% | 39.11% |

The exact-turn results are especially weak for preference and temporal
reasoning: preference has no turn hit at any reported k, while temporal
Recall-any@10 is 3.94%. These are retrieval/formation observations only; the
missing QA stage prevents any claim about final reasoning accuracy.

## Abstention

| Measure | Result |
|---|---:|
| Abstention cases | 30 |
| Correct abstentions | 0 |
| Abstention accuracy | **0.00%** |

The runner predicts abstention only when retrieval returns no items. It
returned at least one item for every official abstention case, so none were
correctly rejected. This run provides no evidence of a usable abstention
policy.

## Latency

| Operation | Samples | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Retrieval per question | 500 | 6.66 ms | 8.27 ms | 9.42 ms |
| Complete case ingestion | 500 | 265.43 ms | 294.57 ms | 308.83 ms |

These are local in-memory harness timings with a fresh service per question.
They are not PostgreSQL, concurrent, cold/warm, scale, or production latency
evidence. Complete-case ingestion latency also varies with the number of turns
in each case and is not a per-memory throughput measurement.

## Errors, timeouts and release irregularities

- Case errors: 0.
- Timeouts: 0.
- Retrieval rows without the configured channel set: 0.
- Degraded retrieval rows: 0.
- The artifact has no separate unsupported-case field; no retrieval case was
  skipped because all 500 have rows.
- The official release contains 13 cases with duplicate filler session ids and
  12 empty turns. Occurrence-qualified refs prevent duplicate ids from
  collapsing; empty turns are validated but not ingested.
- Sixty-five sessions have clock times later than their question on the same
  date. The adapter treats the published timestamps as day-precision for
  cutoff validation because no affected session is on a later calendar date.

## What the run establishes

The run establishes that the adapter can process all 500 official
`LongMemEval_S` cases without case errors or timeouts, and it measures the
current D.I.V.E formation/retrieval behavior under one frozen configuration.
It exposes three material failures: very low gold-turn formation coverage,
very low exact-turn retrieval, and zero abstention accuracy.

It does **not** establish:

- an official LongMemEval overall or full QA score;
- reader answer correctness or reasoning quality;
- superiority to an official baseline or another memory system;
- semantic embedding quality, because deterministic vectors were excluded
  from semantic dense scoring;
- direct comparability with official raw turn/session retrieval baselines,
  because this runner ranks normalized D.I.V.E memories;
- PostgreSQL, ANN, concurrency or scale behavior;
- production readiness.

The next valid LongMemEval evidence step is a separately configured and costed
reader-generation plus official GPT-4o judge run. Its score must be reported as
a new stage and must not overwrite or reinterpret this retrieval artifact.
