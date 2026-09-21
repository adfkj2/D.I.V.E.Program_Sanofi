# ADR-010 Traceable hybrid retrieval and RRF baseline

Status: Accepted as an experimentable baseline (2026-09-20)

## Context

The MVP always ran most channels inside one SQLite method. It could not report
the marginal benefit, latency or failure mode of dense, predicate, temporal,
entity, relation, or multi-hop retrieval. RRF's constant was a literal.

## Options

1. Dense-only: simple, but weak on exact names and not available in offline CI.
2. Weighted normalized scores: tunable, but normalization is distribution-sensitive.
3. RRF: robust to incompatible score scales and label-sparse data.
4. Learned fusion: potentially better, but currently lacks labels.

## Decision

Keep RRF as the baseline, not an assumed winner. `RetrievalConfig` versions
enabled channels, per-channel depth, weights and `rrf_k`. Namespace and
deletion/status filters are never disableable. Temporal filtering is an
explicit ablation channel. Every request produces a redacted trace with query
hash, executed channels, candidate counts/latencies, raw scores, ranks, final
score and aggregate exclusion reasons.

Multi-hop is default-off. It executes only when both the query intent and an
explicit configuration enable it; the current user-hub topology is not accepted
as production graph evidence.

## Rejected alternatives

- Learned fusion and rerankers are deferred until fixed relevance labels exist.
- Neo4j or another graph database would accelerate an unproven topology.
- Removing BM25 before a real semantic comparison would discard a cheap exact-match baseline.

## Consequences

The A–H runner now makes each channel falsifiable. The six-case offline result
is a harness regression only. Promotion decisions require LongMemEval and a
larger internal held-out set; G/H identity in the first report also makes clear
that context packing is not yet a separate retrieval-stage treatment.
