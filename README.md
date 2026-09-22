# D.I.V.E.Program_Sanofi
首届 _SCU X Sanofi_ 菲凡极客项目 

Long-term interactive AI memory system. Research and design documents live in
[`docs/`](docs/). The first local MVP implements an event-sourced memory
service with selective writing, provenance, temporal-aware projections,
lexical/structured retrieval, deterministic test vectors, deletion, and a
layered evaluation harness. Phase 3 design and evidence are in
[`docs/15-phase-3-architecture-review.md`](docs/15-phase-3-architecture-review.md),
[`docs/16-phase-3-design-options.md`](docs/16-phase-3-design-options.md), and
[`docs/17-phase-3-roadmap.md`](docs/17-phase-3-roadmap.md).

## Local development

```powershell
python -m pytest
```

The core package is dependency-free. The optional HTTP adapter can be enabled
with `pip install -e ".[api]"` and served with `uvicorn` after creating an app
from `dive_memory.api:create_app`.

## MVP status

The local MVP now covers event sourcing, atomic and retryable outbox projection,
selective/deferred extraction, persisted write decisions, provenance, version
chains and deterministic replay, bitemporal filters, profiles, entities and
traceable channel retrieval and configurable RRF, context packing, consolidation,
reinforcement/decay, recoverable and hard deletion, export, persistent user
controls, optional namespace authorization, strict candidate validation,
eight-way immutable memory transitions, generation-aware embedding cutover,
five evaluation baselines, and smoke/load benchmarks:

```powershell
$env:PYTHONPATH = "src"
python -m dive_memory.smoke
python -m dive_memory.benchmark --memories 10000 --queries 20
```

The API adapter and integration tests use the optional development environment:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[api,dev]"
.venv\Scripts\python -m pytest -q
```

Embeddings are supplied through a provider interface. The default is the
offline deterministic **test** provider and does not contribute semantic dense
scores. `OpenAICompatibleEmbeddingProvider` and the lazy local
`LocalSentenceTransformerEmbeddingProvider` return explicit model/revision and
degraded metadata; fallback vectors are never scored as semantic. The local
`EmbeddingLifecycle` supports staged, complete backfill, atomic cutover and
rollback. BGE-M3 is a provisional production candidate—not a benchmark-proven
default. A deployment must configure and validate a semantic provider.

Retrieval defaults to BM25 + predicate + temporal + entity/relation signals;
dense contributes only with a healthy semantic provider. Multi-hop is
default-off because the current user-hub topology has not shown adequate
evidence recall. The offline A–H output is committed at
[`eval/reports/p0-offline-latest.json`](eval/reports/p0-offline-latest.json),
with interpretation in
[`docs/benchmark/p0-offline-report.md`](docs/benchmark/p0-offline-report.md).
It is a six-case harness regression, not a quality leaderboard.

The SQLite adapter remains the local/default CI backend. `PostgresStore` is an
opt-in PostgreSQL + pgvector adapter for the online ingest, retrieve and
memory-delete path of the same domain service. Eight real
PostgreSQL integration tests now cover clean/idempotent migrations, concurrent
`SKIP LOCKED` claims, stale retry limits, projection rollback, concurrent
idempotency, namespace isolation, temporal transitions, hard-purge residue and
the exact pgvector cosine operator:

```powershell
.venv\Scripts\python -m pip install -e ".[postgres,dev]"
$env:DIVE_TEST_POSTGRES_DSN = "postgresql://dive:dive_test_password@127.0.0.1:55432/dive_test?connect_timeout=5"
.venv\Scripts\python -m pytest -q tests\integration\postgres --basetemp .pytest-postgres
```

The measured 10K dense-only exact baseline is ingest 38.62 memories/s and
retrieval p50/p95/p99 167.18/176.78/179.36 ms with 96-d synthetic vectors. It
is a plumbing/performance result, not semantic-model or ANN evidence. See
[`docs/benchmark/postgres-exact-10k-report.md`](docs/benchmark/postgres-exact-10k-report.md),
[`docs/adr/ADR-012-postgresql-pgvector.md`](docs/adr/ADR-012-postgresql-pgvector.md)
and [`ops/postgres/README.md`](ops/postgres/README.md).

The strict LongMemEval-cleaned adapter is implemented and tested against a
small synthetic fixture, and a full **retrieval-stage** run over all 500 official
`LongMemEval_S` cases is committed. That run completed with 0 case errors and 0
timeouts, and it is reported as a retrieval result only:

| Stage | Status |
|---|---|
| Official dataset adapter, 500 cases | `COMPLETED` |
| D.I.V.E formation + retrieval | `COMPLETED` (500/500 rows, 0 errors, 6,850 s = 1.90 h; formation coverage 739/896 = 82.48%) |
| Reader answer generation | `NOT COMPLETED` |
| Official GPT-4o judge | `NOT COMPLETED` |

**No official LongMemEval score is claimed.** See
[`docs/benchmark/longmemeval-methodology.md`](docs/benchmark/longmemeval-methodology.md)
for the evidence boundary and
[`docs/benchmark/longmemeval-report.md`](docs/benchmark/longmemeval-report.md)
for the measured numbers.

### Semantic write gate

The default write gate is the rule-based `utility-baseline-v1`. A calibrated
semantic gate (`semantic-utility-v2.1`, nearest-anchor margin scoring over real
`bge-m3` vectors) is implemented and raises gold evidence-turn formation
**offline at the gate** from 6.14% to ~84.82% (82.48% on the full corpus), and
reduces false accepts in the false-memory suite **from 12 to 0**. Both figures
are gate-level measurements, not end-to-end answer quality. See
[`docs/diagnosis-formation-gate-2026-09-21.md`](docs/diagnosis-formation-gate-2026-09-21.md)
and [`docs/fix-write-gate-semantic-2026-09-21.md`](docs/fix-write-gate-semantic-2026-09-21.md).

The gate encoder is the dominant cost of a full benchmark sweep, so the runner
exposes `--device`:

```powershell
$env:HF_HOME = "eval/external/huggingface"
$env:HF_HUB_OFFLINE = "1"; $env:TRANSFORMERS_OFFLINE = "1"   # required: see below
$py = ".venv-gpu\Scripts\python.exe"                          # torch +cu126
& $py -m dive_memory.longmemeval_benchmark eval/external/longmemeval/longmemeval_s_cleaned.json `
      --output eval/reports/longmemeval-s-retrieval-gpu.json --gate semantic --device cuda
```

Measured on this workstation, from the **completed full run** (500/500 cases):
the GPU finished in **6,850 s = 1.90 h at 13.70 s/case**, against a CPU
projection of 20.5 h — an end-to-end **10.78×**. An earlier 4-case sample gave
13.47× / 1.52 h; the full corpus showed that estimate to be **25% optimistic**,
so cite 1.90 h. GPU and CPU gate decisions are equivalent — the same probe set
yields identical accept/skip/review actions and reason codes, with scores
differing by at most 1.08e-7. Full profile and the projection-error analysis:
[`docs/benchmark/longmemeval-500case-profiling.md`](docs/benchmark/longmemeval-500case-profiling.md).

> `transformers` probes the Hub for a PEFT adapter even when the checkpoint is
> fully cached, so a local-model run must set `HF_HUB_OFFLINE=1` and
> `TRANSFORMERS_OFFLINE=1` or it will fail with `httpx.ProxyError: 502`.

Still unverified: reader QA with the production tokenizer and the official
GPT-4o judge, the preregistered P4-P0-2 embedding comparison on the frozen
project corpus, PostgreSQL RLS/backup erasure, replay/reindex/job/event-delete/
export parity, and the ANN (HNSW/IVFFlat), mixed-workload and 1M experiments.
100K **exact** retrieval *is* measured (p50 156.12 ms) and concurrency is
partially characterized (branch A at 1/2/4/8 workers, 0 failed ops) — but that
concurrency run is unfinished, so it is not a completed result. The repository
must not be described as production-ready until those applicable release gates
pass.

**Phase 4 P0 results are consolidated in
[`docs/20-phase-4-results.md`](docs/20-phase-4-results.md)** — every experiment
with its status, artifact-contract compliance, Wilson intervals for the
false-memory rates, the failure/correction record, and the explicit list of what
Phase 4 does not establish.

Phase 4 is closed as an evaluation milestone. The closure set is
[`docs/21-phase-4-architecture-review.md`](docs/21-phase-4-architecture-review.md)
(14 architecture questions answered from executed evidence),
[`docs/22-phase-4-production-gap-analysis.md`](docs/22-phase-4-production-gap-analysis.md)
(18 readiness domains) and
[`docs/23-phase-4-readiness-checklist.md`](docs/23-phase-4-readiness-checklist.md)
(70 item-level verdicts). **The readiness verdict is `NOT READY`** — 12 of 70
items earn `PASS`, and Phase 4 completion does not imply production readiness.
`docs/22` §5 lists the statements the current evidence does not support.

The HTTP adapter uses strict request schemas and idempotency keys for event and
correction writes. `StaticTokenAuthorizer` provides a service-token namespace
boundary; deployments can supply an IAM-backed implementation of the same
protocol.

## Review status

`docs/13-code-review-and-fixes.md` records a full review of this MVP: eleven
defects (silent supersession of unrelated facts, lexicographic `as_of`
comparison, false abstention on natural questions, unretrievable CJK runs,
a `decay` timezone crash, dry-run side effects, a degenerate bm25 mapping, a
missing transaction rollback, and a cross-namespace correction hole), each with
the evidence that reproduced it and the test that now pins it.

Two helpers back those fixes: `temporal.py` normalises wall-clock input such as
`2025` / `2025-06` / `2025年6月2日` before comparison, and `lexical.py` indexes
CJK character bigrams so memories written without punctuation stay retrievable.
`SQLiteStore.reindex_lexical()` rebuilds the FTS projection, which a database
written before that change needs once.

On Windows hosts whose `%LOCALAPPDATA%\Temp` is not writable, point pytest at a
project-local directory:

```powershell
.venv\Scripts\python -m pytest -q --basetemp .pytest-tmp
```
