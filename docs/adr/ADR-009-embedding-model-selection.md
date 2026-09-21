# ADR-009 Embedding provider and model generations

Status: Accepted lifecycle; provisional evidence-based shortlist (updated 2026-09-20)

## Context

The deterministic SHA-256 vector is reproducible but not semantic. A model
change previously required a destructive full reindex and an API fallback could
be scored as if it came from the configured semantic model.

Phase 4 adds a real-model comparison on a frozen project-owned set of 48
documents and 32 Chinese, English and cross-lingual queries. This is controlled
benchmark evidence, not a production or universal ranking. The full method,
scores, isolated-process resource measurements and limitations are in
`docs/benchmark/embedding-model-comparison.md`.

## Options

- BGE-M3 dense: strong bilingual/multilingual evidence, local deployment, 1024 dimensions.
- multilingual-e5-large-instruct: strong multilingual retrieval, query instructions required.
- gte-multilingual-base: smaller operational footprint; quality must be compared.
- Jina/OpenAI-compatible API: low local ops, but egress, recurring cost and provider drift.

## Decision

No model is declared universally best or production-selected. For the next
controlled validation stage:

- multilingual E5 large instruct is the provisional **quality-first reference**;
  it had the highest measured Top-1 (96.88%), MRR (0.9750) and nDCG@10
  (0.9808) on the 32-query set;
- BGE-M3 remains the **latency-balanced challenger**; its aggregate quality was
  close (93.75% Top-1, 0.9688 MRR) and its observed query p50 was lower than
  E5's (100.6 ms versus 132.4 ms in the combined CPU run);
- GTE multilingual base remains the **efficiency challenger**; it was fastest
  and smallest, but its Top-1 was 87.50% and its temporal Top-1 was only 2/4.

These roles are provisional because one query moves the overall result by
3.125 percentage points and each four-query slice by 25 points. A larger
held-out workload, stronger temporal/conflict coverage, end-to-end QA and
system/lifecycle evidence can change the ordering.

`LocalSentenceTransformerEmbeddingProvider` is a lazy optional adapter; the
dependency-free core does not download models. The deterministic provider
remains test-only. E5 query embeddings must use the recorded instruction
protocol; documents remain unprefixed. GTE is allowed only with pinned model
and remote-code revisions plus the compatible pinned runtime, because its
custom code failed under Transformers 5.17 and requires
`trust_remote_code=True`.

All embeddings use an `EmbeddingResult` envelope containing provider, model,
revision, dimension, normalization, latency, semantic/degraded status and
fallback identity. Fallback vectors are explicitly degraded and are excluded
from dense scoring. `EmbeddingLifecycle` stages a pinned generation, resumes
backfill, verifies complete non-degraded coverage, atomically cuts over, and
can backfill/rollback to the previous registered provider. New writes remain on
the active generation, so different generations never mix in one query.

## Rejected alternatives

- Hash vectors are not a production semantic backend.
- Sparse and ColBERT heads of BGE-M3 are deferred until dense ablation exists.
- Binding domain code to a vendor SDK is rejected.
- Declaring BGE-M3 the winner from literature or the earlier design preference
  is rejected now that local evidence shows E5 leading this controlled set.
- Declaring E5 universally best from 32 project-authored queries is rejected.
- Selecting GTE solely from latency/size is rejected until its temporal gap is
  retested on a larger set.

## Consequences

Production readiness fails for deterministic or degraded active vectors. The
same generation-isolation, complete non-degraded backfill and rollback rules
apply whichever candidate is staged.

The measured set verifies only the reported local ordering. It does not cover
large-corpus pollution, PostgreSQL/ANN behavior, sustained concurrency,
formation/evolution quality, reader answers, GPU deployment, model server
operations or SLOs. Final selection requires a larger held-out bilingual and
cross-lingual benchmark, repeated hardware measurements and real lifecycle
backfill/cutover/failure evidence. Until then the provider boundary and
generation metadata are the stable architectural decisions; the model name is
not.
