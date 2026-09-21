# ADR-012 PostgreSQL + pgvector exact search before ANN

Status: Exact-search baseline accepted; ANN decision deferred (2026-09-20)

## Context

SQLite is appropriate for deterministic local development but cannot prove the
production transaction, worker-locking, isolation, purge or pgvector path.
Reference migrations alone were also insufficient. The first PostgreSQL
adapter therefore needed to preserve current domain behavior and produce a
measured baseline before selecting HNSW, IVFFlat or a separate vector service.

## Options

1. PostgreSQL filtered exact cosine search: no ANN build/training and exact
   ordering, but scan cost grows with each namespace's candidate set.
2. pgvector HNSW: strong speed/recall trade-off and incremental construction,
   but higher memory/build/update cost and post-filter recall concerns.
3. pgvector IVFFlat: smaller and tunable, but requires representative training
   and careful probe/recall management under updates and tenant filters.
4. External vector/search service: independent scaling, but adds dual-write,
   consistency, deletion and operations complexity without measured need.

## Decision

Keep PostgreSQL as the production-path source of truth and use pgvector cosine
exact search as the first reference. Dense-only Top-K is executed in PostgreSQL
with `<=>`; the deterministic default provider never masquerades as semantic.
SQLite remains the default offline CI adapter. No ANN index is selected.

The decision is backed by eight real PostgreSQL integration tests covering
migrations, idempotency, `SKIP LOCKED`, stale claim recovery, transaction
rollback, namespace isolation, temporal evolution, hard-purge residue and the
pgvector operator. The 10K single-namespace run measured 176.78 ms retrieval
p95 and 38.62 end-to-end ingests/s with 96-d synthetic vectors.

## Rejected for now

- HNSW and IVFFlat: no 100K/1M recall/latency/build/update evidence exists.
- Qdrant/Milvus/OpenSearch: no measured PostgreSQL bottleneck justifies a second
  persistence/search system.
- Treating deterministic vectors as model evidence: the 10K run is a plumbing
  and system baseline, not a semantic benchmark.

## Consequences

There is now an executable PostgreSQL adapter for the measured online path, a
reproducible container runbook and an exact-vector performance floor. Exact
scan remains simple and reversible.

This is not a production-readiness declaration. PostgreSQL RLS, backup erasure,
connection-loss fault injection, restore testing, real bilingual embeddings,
hybrid-path scale, container RSS, concurrent load, lifecycle/admin method parity
and 100K/1M ANN comparisons remain open. The dense-only SQL Top-K number must
not be cited as hybrid RRF latency.
