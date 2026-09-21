# Embedding model comparison: controlled CPU benchmark

Date: 2026-09-20  
Status: **COMPLETED for the frozen controlled set; final model selection remains open**  
Primary raw result: `eval/reports/embedding-comparison-latest.json`  
Dataset: `eval/datasets/embedding_v1/memory_retrieval.json`

## Evidence boundary

This benchmark compares three pinned multilingual embedding models, a
deterministic hash fixture and a project-owned BM25 reference on exactly the
same 48-document, 32-query corpus. It is reproducible E4 evidence for this
small controlled workload. It is not an external leaderboard, a representative
production sample, a PostgreSQL latency test, an answer-quality evaluation or
a universal model ranking.

The dataset has four queries in each of eight memory-specific categories:
semantic paraphrase, preference retrieval, old episodic recall, temporal
change, conflicting facts, near-duplicate memory, entity ambiguity and
long-tail facts. It includes Chinese, English and cross-lingual cases. One
query changes an overall percentage by 3.125 points, and one query changes a
slice result by 25 points. The point estimates therefore have high
uncertainty; differences are not statistical significance claims.

## Protocol

- CPU-only Windows 11 host: 8 physical / 16 logical cores and 16.44 GB system
  RAM.
- Python 3.12.14, PyTorch 2.14.0+cpu, Transformers 4.39.1 and
  sentence-transformers 3.0.1.
- Batch size 8, L2-normalized float32 vectors and exact in-memory cosine
  ranking through normalized dot product.
- The corpus, query set, top-k values and metric implementation are shared
  across all candidates. No separate metadata filter is applied.
- `multilingual-e5-large-instruct` queries use exactly
  `Instruct: Given a user memory query, retrieve relevant passages that answer the query`
  followed by `Query: <text>`. Documents receive no instruction.
- Model revisions are immutable pins:
  - `BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181`
  - `intfloat/multilingual-e5-large-instruct@274baa43b0e13e37fafa6428dbc7938e62e5c439`
  - `Alibaba-NLP/gte-multilingual-base@9bbca17d9273fd0d03d5725c7a4b0f6b45142062`
  - GTE remote code: `Alibaba-NLP/new-impl@40ced75c3017eb27626c9d4ea981bde21a2662f4`

The combined run is authoritative for quality because every model is evaluated
through one frozen configuration. It loads models sequentially, so its
absolute process RSS is contaminated by allocator retention. CPU-memory
comparisons below use only the three fresh-process artifacts. The combined
run's query latency remains a useful single-host observation, but it is not a
service SLO.

Equivalent PowerShell reproduction commands, from the repository root with
the recorded `.venv-models` environment, are:

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:PYTHONPATH = (Resolve-Path "src")
.\.venv-models\Scripts\python.exe -m dive_memory.embedding_benchmark eval\datasets\embedding_v1\memory_retrieval.json --output eval\reports\embedding-comparison-latest.json --models bge-m3 multilingual-e5-large-instruct gte-multilingual-base --device cpu --batch-size 8
```

For a fair RSS comparison, repeat the command three times in fresh processes,
passing one model after `--models` and a distinct isolated output filename.

## Quality results

| Candidate | Top-1 | Recall@5 | MRR | nDCG@10 | Temporal@1 | Conflict@1 | Query p50 | Model files |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Deterministic SHA-256 fixture | 34.38% | 43.75% | 0.3957 | 0.3890 | 0% | 25% | 0.04 ms | 0 |
| BM25 word/CJK-bigram reference | 65.63% | 84.38% | 0.7438 | 0.7664 | 0% | 50% | 0.09 ms | 0 |
| BGE-M3 | 93.75% | 100% | 0.9688 | 0.9769 | 100% | 50% | 100.6 ms | 2.32 GB |
| multilingual E5 large instruct | **96.88%** | 100% | **0.9750** | **0.9808** | 100% | **100%** | 132.4 ms | 1.14 GB |
| GTE multilingual base | 87.50% | 100% | 0.9375 | 0.9539 | 50% | 75% | **43.3 ms** | **628 MB** |

Top-1 is also Recall@1 here because each query has one relevant document.
All three neural models reached Recall@5 = 100%, but a 48-document corpus is
not evidence that this will hold under memory growth.

On this set, E5 has the highest aggregate quality: 31/32 Top-1 hits versus
30/32 for BGE-M3 and 28/32 for GTE. BGE-M3 is close on aggregate quality and
its observed query p50 is about 24% lower than E5's. The two BGE-M3 Top-1
misses are both in the four-case conflicting-facts slice; that 50% slice result
is a warning, not a stable population estimate. E5 misses one semantic
paraphrase case.

GTE is the speed/footprint leader, but it is not the quality leader. Its
temporal Top-1 is 2/4, compared with 4/4 for BGE-M3 and E5. It also misses one
conflicting-facts and one near-duplicate case. The temporal result is too small
to estimate a general failure rate, but it is material enough to block a
quality-first promotion without a larger temporal set.

The BM25 row is a useful lexical reference, not a substitute for the semantic
models. The hash row confirms that the deterministic test embedder has no
semantic-production claim.

## Isolated-process resource results

Each row below comes from a separate process with a roughly 307–308 MB starting
RSS. `Peak RSS increment` is peak observed RSS minus that process's pre-load
RSS. It is the fair comparison field; the absolute RSS values from the combined
run must not be compared across sequential model loads.

| Model | Load | Documents/s | Query batch/s | Single-query p50 / p95 / p99 | Peak RSS increment | Model files |
|---|---:|---:|---:|---:|---:|---:|
| BGE-M3 | 4.28 s | 24.47 | 31.52 | 104.8 / 120.8 / 129.8 ms | 2.45 GiB | 2,320,496,796 B |
| multilingual E5 large instruct | 3.37 s | 24.86 | 16.18 | 133.9 / 140.9 / 149.4 ms | 2.44 GiB | 1,141,981,124 B |
| GTE multilingual base | 2.72 s | 58.92 | 79.03 | 44.1 / 47.2 / 50.1 ms | 1.52 GiB | 627,846,556 B |

These measurements cover a warmed model and very small batches on one CPU
host. Load time may benefit from the local model cache. RSS is sampled at
defined runner checkpoints rather than continuously profiled, so it can miss a
short-lived peak. No GPU result exists. Vector dimensions are 1024 for BGE-M3
and E5 and 768 for GTE; float32 corpus-vector storage therefore also differs.

## Decision and claim status

- **VERIFIED within this exact dataset/configuration:** E5 has the highest
  measured quality; BGE-M3 is close and faster; GTE is fastest and smallest but
  weaker on the temporal slice.
- **PARTIALLY VERIFIED as a project model choice:** real local models completed
  a shared memory-specific test, but the dataset is only 32 queries and is
  project-authored.
- **NOT VERIFIED:** that E5, BGE-M3 or GTE is universally best, that any model
  meets a production SLO, or that its ranking advantage survives large-corpus,
  PostgreSQL, end-to-end QA, formation/evolution or adversarial workloads.

ADR-009 therefore makes E5 the provisional quality-first reference for the
next validation stage, retains BGE-M3 as the close latency-balanced challenger,
and retains GTE as the efficiency challenger. It does not make a final or
production selection. A larger held-out bilingual/cross-lingual set, stronger
temporal/conflict coverage, repeated timing, lifecycle/backfill tests and
end-to-end retrieval/answer evaluation are still required.

## Failure evidence

Two failures occurred before the successful runs and are retained rather than
silently omitted:

- `docs/benchmark/failures/embedding-bge-m3-download-attempt.md`: redundant
  runtime-format downloads and repeated disconnects in the initial BGE-M3
  snapshot attempt; resolved for the benchmark runner with a runtime-only file
  whitelist.
- `docs/benchmark/failures/embedding-gte-transformers-5-17.md`: GTE remote-code
  execution failed under Transformers 5.17 with an out-of-bounds `IndexError`;
  the pinned, model-card-compatible Transformers 4.39.1 environment completed.

These are dependency/distribution findings. They do not change the quality
scores, but they are part of the operational selection evidence.

## Artifact integrity

| Artifact | SHA-256 |
|---|---|
| `eval/datasets/embedding_v1/memory_retrieval.json` | `10b8177e4203c60a6c2a1b693ab9d5e9441d627f41fbc97d2992e4124b6508d0` |
| `eval/reports/embedding-comparison-latest.json` | `ec5d7de4ac3be4e7da7e194a16b5db209efc303e638e7ba95aaafb9daf077c9a` |
| `eval/reports/embedding-bge-m3-latest.json` | `6a1f2e454793cecc6a9eb8b2d99047c2cf54ae4e9ecd5d705f1f1a7b6ac6f696` |
| `eval/reports/embedding-e5-latest.json` | `d741b59a13f5959cf876d3bba039322514267dfdcad95b8c2251f6867428f91b` |
| `eval/reports/embedding-gte-latest.json` | `f78d6475ed590640838a2204a4a9d04138a641bfabd5a89a8bd8b400f862b97f` |

All report artifacts record baseline Git commit
`16f213464aab60c3e14563ede4109087a14fcaf4` with a dirty worktree. That state is
part of the manifest and must not be rewritten as a clean-release benchmark.
