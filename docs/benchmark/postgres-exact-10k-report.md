# PostgreSQL + pgvector 10K exact-search baseline

Date: 2026-09-20  
Raw result: `eval/reports/postgres-exact-10k-latest.json`  
Command: `python -m dive_memory.postgres_benchmark --memories 10000 --queries 20`

## Environment and scope

- PostgreSQL 16.15 in `pgvector/pgvector:pg16`
- pgvector 0.8.6
- Windows 11 host, Python 3.14.0
- 10,000 memories in one isolated schema and namespace
- 96-dimensional deterministic hash fixture wrapped in a semantic test envelope
- dense-only exact cosine search using PostgreSQL `vector <=> query`
- one ingest/query client; 20 measured queries after one warm-up

The schema was created from all seven migrations for this run and dropped on
completion. The DSN and credentials are not present in the artifact.

## Results

| Measurement | Result |
|---|---:|
| End-to-end ingest throughput | 38.62 memories/s |
| Ingest p50 | 25.54 ms/event |
| Ingest p95 | 30.95 ms/event |
| Ingest p99 | 34.21 ms/event |
| Exact retrieval p50 | 167.18 ms/query |
| Exact retrieval p95 | 176.78 ms/query |
| Exact retrieval p99 | 179.36 ms/query |
| Exact-string Top-1 fixture hit rate | 20/20 |
| Tables plus indexes | 55,951,360 bytes |
| Indexes | 22,773,760 bytes |

Ingest is the complete synchronous path: event, outbox, write decision, memory,
vector, key, version and transition projections. Retrieval records access rows,
so the latency is service-level rather than a bare SQL microbenchmark.

## Interpretation

This run establishes a reproducible correctness/performance reference for the
PostgreSQL exact-vector path. It does **not** establish semantic retrieval
quality: the vectors are deterministic fixtures, and the Top-1 check uses exact
text. It also does not justify HNSW or IVFFlat. At 10K, measured exact retrieval
is below the provisional 500 ms p95 target, so the evidence does not require an
ANN index yet.

The optimized SQL Top-K path currently applies to dense-only retrieval. The
hybrid RRF path still materializes a broader filtered set before application
fusion and is not represented by this latency table. Container RSS, concurrent
load, skewed multi-tenant filters, update/delete throughput, real 1024-d model
vectors, 100K/1M scale and ANN recall are unmeasured.

## Reproduction boundary

Use the opt-in environment in `ops/postgres/README.md`. Default CI remains
offline and skips PostgreSQL. A failed benchmark still drops its unique schema;
it never targets `public` for cleanup.
