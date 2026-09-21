# Phase 3 P0 offline regression report

Date: 2026-09-20  
Backend: in-memory SQLite  
Dataset: project-owned `dive-internal-retrieval-v1`, 6 queries / 4 events  
Embedding: `deterministic-sha256-v1` (dense semantic scoring disabled)  
Raw report: `eval/reports/p0-offline-latest.json`

| Variant | Recall@5 | Precision@5 | MRR | nDCG@10 | Temporal Acc | Abstention F1 |
|---|---:|---:|---:|---:|---:|---:|
| A BM25 | 0.00 | 0.00 | 0.00 | 0.000 | 0.00 | 0.286 |
| B Dense | 0.00 | 0.00 | 0.00 | 0.000 | 0.00 | 0.286 |
| C BM25 + Dense | 0.00 | 0.00 | 0.00 | 0.000 | 0.00 | 0.286 |
| D + Predicate | 0.80 | 0.16 | 0.80 | 0.800 | 1.00 | 0.667 |
| E + Temporal | 0.80 | 0.16 | 0.80 | 0.800 | 1.00 | 0.667 |
| F + Entity/Relation | 0.80 | 0.16 | 0.80 | 0.800 | 1.00 | 0.667 |
| G + Multi-hop | 0.90 | 0.20 | 1.00 | 0.923 | 1.00 | 1.000 |
| H Full candidate generation | 0.90 | 0.20 | 1.00 | 0.923 | 1.00 | 1.000 |

## Interpretation

This run proves the harness and makes a useful negative result visible: neither
BM25 nor the hash vector can answer paraphrased predicate questions in this tiny
fixture. Predicate lookup supplies most positive recall. G finds only one of
two required multi-hop evidence memories (slice Recall@5 = 0.5), so the current
star traversal remains default-off. G and H are identical because packing is
outside candidate-retrieval evaluation.

These six authored cases are far too small for an architecture selection and
the latency samples are not system benchmarks. No semantic-embedding,
LongMemEval, PostgreSQL, ANN, QA or production claim follows from this table.

## Local SQLite 10K system smoke after resolver indexing

Command: `python -m dive_memory.benchmark --memories 10000 --queries 20`  
Environment date: 2026-09-20, Windows, in-memory SQLite, deterministic 96-d test vector

| Operation | p50 | p95 | p99 |
|---|---:|---:|---:|
| ingest (ms/event) | 0.291 | 0.530 | 0.617 |
| retrieve (ms/query) | 341.45 | 355.38 | 355.57 |

The benchmark first exposed an O(n²) resolver implementation that loaded all
active memories per ingest. The final run uses the indexed `memory_keys`
projection. Its embedded quality smoke has only one answerable and one negative
case (recall 1.0, abstention accuracy 1.0, false-memory rate 0, provenance 1.0),
so those figures validate wiring only. This is not PostgreSQL or semantic-model
evidence.

The later, separately labelled PostgreSQL 10K exact-search run is documented in
`docs/benchmark/postgres-exact-10k-report.md`; it does not change the P0 quality
interpretation above.
