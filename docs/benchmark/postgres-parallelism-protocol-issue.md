# PostgreSQL Parallelism Protocol Issue

## Discovery

The completed recovery run `postgres-100k-concurrency-20260921-110255` used
different timed query targets for Branch A and Branch B. The recovery runner
constructed Branch B's random seed with an additional `+1000` offset:

```python
self.config.query_seed + workers + (1000 if branch is BRANCH_B else 0)
```

Consequently, the A1 and B1 timed query IDs were not identical. B1 is retained
as immutable evidence and is not reclassified as a failed workload run.

## Impact

The following B1 evidence remains valid:

- the session-scoped `max_parallel_workers_per_gather = 0` override took effect;
- parallel execution disappeared from the independently captured query plan;
- the Branch B workload executed successfully;
- the correctness oracle passed; and
- the database and container remained healthy.

The following causal claims are not supported by A1/B1:

- disabling parallelism caused the observed throughput change;
- disabling parallelism caused the observed latency change; or
- the A1/B1 performance delta estimates the effect of parallelism.

The target mismatch is a confounder, so those performance values may only be
reported descriptively within their original arms.

## Status

Branch B1:

`VALID_OPERATIONAL_EVIDENCE`

`INVALID_FOR_CAUSAL_PERFORMANCE_COMPARISON`

Branch B2/B4/B8 remain not run. The existing recovery run and all of its
artifacts remain immutable; the paired experiment uses a new runner, code
fingerprint, run ID, schedule, and artifact tree.
