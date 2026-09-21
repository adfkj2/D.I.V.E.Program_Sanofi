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
small synthetic fixture. No official LongMemEval score is claimed; see
[`docs/benchmark/longmemeval-methodology.md`](docs/benchmark/longmemeval-methodology.md).

Still unverified: official LongMemEval, real BGE-M3/e5/GTE comparison, reader
QA with the production tokenizer, PostgreSQL RLS/backup erasure, concurrent
load, replay/reindex/job/event-delete/export parity, and 100K/1M
exact-versus-HNSW/IVFFlat experiments. The repository must not be described as
production-ready until those applicable release gates pass.

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
