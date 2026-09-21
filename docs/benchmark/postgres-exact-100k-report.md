# PostgreSQL exact 100K benchmark report

Run ID: `postgres-100k-20260921-084800`  
Status: **PARTIAL — stopped at Checkpoint G**

## Scope

This is a 100,000-row PostgreSQL + pgvector synthetic-vector infrastructure benchmark. It does not measure semantic embedding quality or real-world recall.

## Environment

The recorded PostgreSQL, pgvector, Docker, host and database settings are in `environment.json`. At 8 workers PostgreSQL exhausted the container's 64 MiB dynamic shared-memory mount; the database volume remained healthy with ample free space.

## Dataset

The main namespace contains exactly 100,000 memories and 100,000 96-dimensional deterministic synthetic vectors. Metadata provides 1%, 10% and 50% cohorts; temporal windows are deterministic. See `dataset.json`.

## Methodology

Every stage ran as a separate command and published a JSON artifact. Fresh-connection first-query is a proxy, not true cold cache. No HNSW or IVFFlat index was created. The experiment stopped on sustained concurrency errors and did not run the mixed workload.

## Bulk Load

Generation took 5.69 s; database insert took 4.93 s (20279.41 rows/s). This COPY rate is not production ingest throughput.

## Incremental Ingest

Single service writes achieved 57.49 memories/s with p50 16.00 ms and p95 25.64 ms. Four writers achieved 177.31 memories/s with zero failures; idempotency passed.

## Exact Retrieval

100 warm service queries measured p50 156.12 ms, p95 165.93 ms and p99 169.06 ms. The exact-string fixture Top-1 rate was 100%. The fresh-connection first-query proxy was 184.06 ms.

## Filtered Retrieval

- 1% / 1,000 candidates: p50 20.01 ms.
- 10% / 10,000 candidates: p50 30.87 ms.
- 50% / 50,000 candidates: p50 81.51 ms.

These are direct SQL infrastructure measurements because the service predicate channel is not a hard metadata filter.

## Temporal Retrieval

- Current (80,000 eligible): p50 301.25 ms.
- Historical (5,555 eligible): p50 21.06 ms.
- Window overlap (24,970 eligible): p50 92.29 ms.

All direct-SQL fixture results and service current/historical checks were correct. SQL performance and service correctness are separate evidence.

## Delete

On an isolated fixture, soft delete took 16.28 ms and hard purge 19.37 ms. Row/vector/projection cleanup, tombstones and rollback passed; the main 100K namespace was untouched.

## Concurrency

1 worker: 18.92 q/s. 4 workers: 60.17 q/s. At 8 workers, 44 of 64 operations failed because PostgreSQL could not resize dynamic shared-memory segments in `/dev/shm`. `SKIP LOCKED` disjoint-claim correctness passed. The checkpoint status is FAILED.

## Mixed Workload

**NOT COMPLETED.** The planned 70/20/10 read/ingest/delete workload was not run after Checkpoint G crossed the sustained-error stop condition.

## Resource Usage

Host memory, benchmark-client RSS and Docker container snapshots remain separately named in `resource-usage.json`. The database volume had ample space; `/dev/shm` was the saturated resource during concurrency.

## Database Size

The run schema occupied 167550976 bytes: tables 122273792, indexes 44769280, TOAST 172032. This is a 96-d result; 768/1024/1536 values in `database-size.json` are labelled CALCULATED ESTIMATE.

## Query Plans

Representative `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` evidence for exact, three filter selectivities and three temporal treatments is in `query-plans.json`.

## Comparison With 10K

The old 10K service baseline was 38.62 memories/s and retrieval p50/p95/p99 167.18/176.78/179.36 ms. The 100K retrieval result is **not directly comparable** because the 100K main corpus was bulk-loaded without the full event/provenance projections, runtime state differed, and the sample count changed from 20 to 100. No linear-scaling claim is made.

## Observed Bottlenecks

Exact scan/sort and service hydration dominate individual retrieval. Under concurrency, the first hard saturation was Docker's 64 MiB `/dev/shm` allocation for PostgreSQL parallel work.

## Limitations

- Synthetic 96-d vectors; no semantic-quality claim.
- Cache state was not controlled at PostgreSQL shared buffers and OS page cache.
- Resource measurements are point snapshots.
- Filter/temporal SQL timing is separate from current service behavior.
- Mixed workload was not run because the experiment stopped at G.

## Evidence Level

A-F are completed local evidence; G is a retained failure; H is explicitly NOT_COMPLETED. This is a partial benchmark, not a successful end-to-end 100K workload certification.

## What This Benchmark Proves

It proves the recorded local behavior for 100K bulk storage, exact retrieval, SQL filtering, temporal paths, real service ingest, deletion and the concurrency failure boundary.

## What This Benchmark Does NOT Prove

It does not prove semantic retrieval quality, a successful mixed workload, production SLOs, true cold-cache behavior, ANN behavior, 1M scaling, or that PostgreSQL generally can or cannot scale.

## Required Questions

- **1_10K_to_100K_exact_latency**: Observed 100K p50 156.12 ms versus 10K p50 167.18 ms (0.93×); this is not directly comparable.
- **2_is_scaling_linear**: No linear conclusion is supported: the observed ratio was not near 10× and the load/projection protocols and runtime state differ.
- **3_dominant_time_component**: Exact vector scan/sort plus service candidate hydration dominates the service path; representative plans are preserved.
- **4_metadata_filter_workload_reduction**: The 10% treatment reduced eligible candidates from 100,000 to 10,000 (90%); 1% and 50% treatments behaved consistently.
- **5_temporal_extra_cost**: Current-fact temporal SQL p50 was 301.25 ms versus exact service p50 156.12 ms; paths differ, so this is not a pure overhead ratio.
- **6_planner_behavior**: Observed plan node types: Bitmap Heap Scan, Bitmap Index Scan, Gather Merge, Hash, Hash Join, Index Scan, Limit, Nested Loop, Seq Scan, Sort; no ANN index existed.
- **7_bulk_vs_incremental**: COPY achieved 20279.41 rows/s while single-request service ingest achieved 57.49 memories/s; protocols are intentionally not equated.
- **8_concurrency_first_saturation**: Dynamic shared memory (/dev/shm), not the database volume, saturated first at 8 workers; 44/64 operations failed and the experiment stopped.
- **9_memory_footprint**: Final run schema size was 167550976 bytes (1675.5 bytes per main memory including schema/fixture overhead) for 96-d vectors.
- **10_current_largest_bottleneck**: At single-query load the dominant path is exact vector scan/sort and service hydration; under concurrency the hard bottleneck was the container's 64 MiB dynamic shared-memory mount.
