# PostgreSQL 100K Concurrency Recovery Experiment — Preregistration

Status: **PREREGISTERED — NO RECOVERY BENCHMARK RESULTS OBSERVED**  
Preregistered: 2026-09-21 (Asia/Shanghai)  
Immutable source run: `postgres-100k-20260921-084800`  
New run family: `eval/reports/postgres-100k-concurrency/<new-run-id>/`

## Research question

Was the original 8-worker failure primarily caused by the Docker container's 64 MiB `/dev/shm`, or does a PostgreSQL query-plan, query implementation, connection, or concurrency defect remain after that environmental constraint is removed?

This experiment does not erase, continue, or relabel the original run. The original run remains PARTIAL, Checkpoint G remains FAILED, and Checkpoint H remains NOT_COMPLETED.

## Frozen prior evidence

- Dataset: 100,000 memories and 100,000 deterministic 96-dimensional synthetic vectors, seed `20260920`, namespace `postgres-100k-main`.
- Original container `/dev/shm`: 64 MiB.
- Original 8-worker result: 64 operations, 20 successes, 44 failures (68.75% error rate), with PostgreSQL dynamic shared-memory `DiskFull` errors.
- The original artifacts and manifest will not be modified.

## Hypotheses

### H1 — constrained shared memory is the primary cause

The original 8-worker failure was primarily caused by Docker's default 64 MiB `/dev/shm`. With the same workload and PostgreSQL parallelism retained, increasing `/dev/shm` to 512 MiB should remove sustained DSM errors and reduce the 8-worker error rate from 68.75% to at most 5% (an absolute reduction of at least 50 percentage points).

### H2 — concurrency degrades latency without mass correctness failure

Even with larger shared memory, increasing concurrency may increase p95/p99 latency or flatten throughput. At least one 8-worker configuration should nevertheless complete with at most 5% operation failures, no wrong read results, no write-count mismatch, no duplicate/idempotency violation, no rollback violation, no delete violation, no database/container crash, and no shared-memory exhaustion.

Latency degradation is reported descriptively; it is not itself a failure unless accompanied by a stop condition. Saturation is the first level at which throughput fails to improve by at least 10% while p95 latency rises by at least 25% relative to the preceding level, or the first level that triggers a stop condition.

### H3 — failure after enlargement is evidence against a Docker-only explanation

If, with 512 MiB `/dev/shm`, either branch has at least three DSM/shared-memory errors at one worker level, more than 20% operation failures, a database/container crash, OOM, transaction corruption, or sustained connection failures, the original issue cannot be attributed solely to Docker's default shared-memory size. The exact error classes and affected operations will be retained.

## Independent variables

1. Worker count: 1, 2, 4, 8. Sixteen workers are optional and will only be considered if 8 workers is stable, host/container pressure is low, and the preregistered saturation rule has not fired.
2. Query parallelism:
   - Experiment A: existing PostgreSQL parallel settings unchanged.
   - Experiment B: benchmark-session-only `SET max_parallel_workers_per_gather = 0` on every workload connection.
3. Container shared memory: historical control is the immutable 64 MiB run; both new branches use an explicitly configured 512 MiB `/dev/shm`.

## Controlled variables

- The existing validated 100K dataset and schema are reused; no new distribution is generated.
- Required invariants before each branch: 100,000 main-namespace memories, 100,000 one-to-one vectors, vector dimension 96, seed `20260920`, and the recorded metadata distribution (1,000 / 10,000 / 50,000 cohort members).
- Same PostgreSQL image/version, pgvector extension, database volume, query text, top-k, query vectors, connection model (one connection per worker), eight retrieval operations per worker, client host, and measurement implementation.
- No ANN index, timeout inflation, automatic retry, or adaptive worker reduction is allowed.
- Experiment B changes only the session-level parallel-gather setting; no system or production default is changed.

## Environment choice

The planned recovery container uses 512 MiB `/dev/shm`. This is eight times the failing allocation, but only about 3.1% of the recorded 16.44 GB host RAM, so it is deliberately bounded rather than maximal. The new container will be separately named and will reuse the validated PostgreSQL data volume while the old container is stopped. Host RAM, Docker allocation, effective container memory limit, mount, image, PostgreSQL version, pgvector version, and relevant settings will be captured before workload execution.

## Pre-run inspection

Before either branch, capture:

- `SHOW max_parallel_workers`, `max_parallel_workers_per_gather`, `max_worker_processes`, `work_mem`, `shared_buffers`, and `effective_cache_size`;
- `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for the exact retrieval query with parallelism enabled and disabled;
- whether the plans contain `Gather`, `Gather Merge`, parallel-aware nodes, planned workers, and launched workers;
- `/dev/shm` total and used at idle.

These go to `environment-before.json`, `postgres-settings.json`, and `parallel-query-plan.json`. The causal interpretation will be based on the captured plans, not an assumption that pgvector itself consumes the shared memory.

## Workload protocol

For each branch, run 1, 2, 4, and 8 workers in order. Each worker opens one independent connection and executes eight deterministic exact-cosine top-5 retrievals against the same 100K main namespace. No failed operation is retried. A fixed seed controls query selection. A correctness oracle verifies result IDs against a single-session, parallel-disabled exact result for every query vector before timed concurrency begins.

At every level record:

- operations, successes, failures, error rate, throughput;
- retrieval and overall p50/p95/p99 latency;
- ingest p50/p95/p99 as NOT_APPLICABLE for retrieval-only levels;
- exact error, frequency, operation/query identity and error class;
- host CPU/memory, container CPU/memory, benchmark-client RSS;
- PostgreSQL connections, active queries, lock waits, deadlocks, retries and timeouts;
- `/dev/shm` total/used at idle and after each level when measurable, otherwise `NOT_MEASURED` with a reason;
- database/container state and post-level row/vector/count integrity.

Correctness probes outside the timed retrieval curve will verify deterministic read consistency, transactional rollback, idempotent duplicate prevention, write row counts, and soft/hard delete behavior in a run-isolated namespace. A returned success counts as successful only if its result satisfies the oracle.

## Failure criteria and stop conditions

Stop the current configuration immediately, preserve its artifact and do not run a higher worker count if any of the following occurs:

- database or container crash/restart;
- any data-corruption or correctness mismatch;
- operation failure rate greater than 20%;
- at least three DSM/shared-memory errors at a worker level;
- OOM, disk free space below 2 GB, host available memory below 1 GB, or Docker/container memory pressure above 90%;
- sustained connection failures (three or more at a worker level).

Do not lower concurrency, raise timeouts, or add retries after a failure. Missing resource measurements are marked `NOT_MEASURED`, never estimated.

## Mixed-workload gate and frozen definition

The mixed workload may run only if at least one 8-worker branch has at most 5% errors, zero correctness mismatches, zero database/container crashes, and zero DSM/shared-memory errors. If both qualify, select the branch by: zero failures first, then higher throughput, then lower retrieval p95, then lower resource pressure. The selection and all losing-branch results remain visible.

The mixed workload is frozen at:

- 8 workers, 800 total scheduled operations (100 per worker), deterministic seed `20260921`;
- 16 retrieval warmups excluded from measurement;
- 70% exact retrieval, 20% ingest, 10% update/delete, using only operations supported by the existing service API;
- writes use a run-isolated namespace; deletes target only run-created records; no main-dataset row is deleted;
- no retries and no ratio changes after results are seen.

If update cannot be expressed as a supported atomic service operation, the 10% share is deletion and the limitation is recorded; it will not be silently replaced after execution.

Record overall throughput, per-operation p50/p95/p99, failure/retry/transaction rates, locks, connections, CPU, memory, `/dev/shm`, and exact errors. The post-workload integrity audit checks main COUNT, expected inserted/live/deleted counts, duplicate keys, orphan memories/vectors, invalid deleted rows, rollback isolation, transaction consistency, and idempotency state. Any mismatch prevents PASS.

## Interpretation rules

- **Case A:** Experiment A is stable at 8 workers. Conclude only: “Previous failure is strongly attributable to the original constrained shared-memory environment under this workload.”
- **Case B:** A fails but B is stable. Conclude that the current exact-retrieval parallel plan may impose excessive shared-memory pressure in this local container environment; compare disabling parallelism with retaining it plus more shared memory.
- **Case C:** Both fail. Conclude that the issue is not explained by shared-memory allocation alone; investigate connection management, `work_mem`, parallel workers, query implementation, container memory, and PostgreSQL logs. Do not proceed to ANN.
- **Case D:** Both succeed. Compare throughput, latency, and resource pressure; choose an operational benchmark recommendation without changing production defaults.

H1 is supported only by Experiment A meeting its numeric threshold. H2 is supported only if an 8-worker branch meets the correctness/error gate and the latency/throughput curve is reported. H3 is supported if its specified post-enlargement failure evidence occurs; absence of such evidence does not prove universal safety.

## Completion and downstream decisions

The new experiment is COMPLETE only when concurrency A/B, the gated mixed workload, and the integrity audit all complete and pass. The old 100K run always remains PARTIAL. ADR-013 is written after results. The ANN experiment plan is created only if the new G/H evidence closes; ANN itself, 1M scale, and embedding benchmarks are out of scope and will not run.
