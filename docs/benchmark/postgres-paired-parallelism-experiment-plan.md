# PostgreSQL Paired Parallelism Experiment Preregistration

## Question and frozen environment

This experiment estimates the descriptive performance effect of changing only
the session setting `max_parallel_workers_per_gather` for the existing 100,000
row exact-vector workload:

- **P (parallel):** `max_parallel_workers_per_gather = 2`
- **NP (no parallel):** `max_parallel_workers_per_gather = 0`

The recovery container, 512 MiB `/dev/shm`, PostgreSQL 16.15, pgvector 0.8.6,
100,000 memories, 100,000 96-dimensional vectors, namespace, metadata,
Top-K, connection strategy, timeout, client code, correctness oracle, retry
policy, and all other PostgreSQL settings are held fixed. The experiment does
not revisit the cause of the earlier shared-memory failure.

## Registered design

- Worker levels: **1, 4, and 8**.
- Trials per worker level: **4**.
- Timed operations: **32 per worker per arm**.
- Warmup: **2 operations per worker per arm**, excluded from all timed metrics.
- Trial order is exactly counterbalanced:
  - Trial 1: P -> NP
  - Trial 2: NP -> P
  - Trial 3: P -> NP
  - Trial 4: NP -> P
- There are no workload retries and no adaptive configuration changes.

A single deterministic `query-schedule.json` is generated before any formal
arm runs. Its entries contain phase, worker level, trial, worker ID, operation
index, target memory ID, query parameters, and schedule seed. It contains no
configuration/arm field. P and NP select their entries from this one stored
schedule; execution code never samples targets. The canonical SHA-256 excludes
only the document's own hash field. Every arm artifact records that hash, and
a mismatch blocks execution or invalidates the pair automatically.

`execution-order.json` freezes arm order before measurement. Each invocation of
the formal command runs at most one requested paired trial. Each arm is stored
atomically and separately, so an interrupted command can resume without
repeating a completed PASS arm.

## Preflight and correctness gates

Before timed work, each arm verifies its effective session setting with `SHOW`
and runs `EXPLAIN (ANALYZE, FORMAT JSON)` using a scheduled warmup query.

- P must contain parallel execution and at least one launched worker.
- NP must contain no `Gather`, `Gather Merge`, parallel-aware node, parallel
  node, planned worker, or launched worker.

A formal trial stops before timed measurement when either plan gate fails.
Small-fixture smoke testing does not require PostgreSQL to choose a parallel P
plan, but it still validates the settings, the NP plan prohibition, and exact
schedule identity.

Every operation requires the known exact target memory to be the top result.
The memories/vectors row counts must be unchanged across an arm. Any incorrect
read, unexpected row delta, or schedule/hash mismatch makes the arm and pair
invalid regardless of performance. Failed or invalid pairs are excluded from
aggregation.

## Metrics and analysis

Each worker/trial/configuration artifact records operations, successes,
failures, failure rate, throughput, p50, p95, p99, minimum, maximum,
p95-p50, p99-p50, correctness, timeouts, deadlocks, DSM/shared-memory errors,
connection errors, retries (fixed at zero), database/container health, and
resource snapshots before and after.

Each valid pair reports P minus NP differences for throughput, p50, p95, and
p99. Per worker level, the summary reports medians and ranges across valid
trials, plus paired direction counts. Conclusions are based on direction
consistency across paired trials, never a single best run or only a pooled
average. With four trials, results remain descriptive; no exaggerated
significance claim is permitted. If directions are inconsistent, the result is
`INCONCLUSIVE`.

## Cache limitation

The protocol cannot fully reset or equalize the operating-system page cache or
PostgreSQL shared-buffer history. Equal warmup and exact counterbalancing reduce
systematic order bias but do not establish perfect cold-cache equivalence.

## Artifact layout

```text
eval/reports/postgres-parallelism/<run-id>/
  manifest.json
  environment.json
  query-schedule.json
  execution-order.json
  workers-1/trial-1/parallel.json
  workers-1/trial-1/no-parallel.json
  ...
  summary.json
```

The old recovery Branch A measurements are historical references only and are
not an arm of this paired experiment.
