# D.I.V.E.Program_Sanofi
首届 _SCU X Sanofi_ 菲凡极客项目 

Long-term interactive AI memory system. Research and design documents live in
[`docs/`](docs/). The first local MVP implements an event-sourced memory
service with selective writing, provenance, temporal-aware projections,
hybrid lexical/deterministic-vector retrieval, deletion, and an evaluation
harness.

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
bounded relation traversal, RRF retrieval, context packing, consolidation,
reinforcement/decay, recoverable and hard deletion, export, persistent user
controls, optional namespace authorization, five evaluation baselines, and
smoke/load benchmarks:

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

Embeddings are supplied through a provider interface. The default remains the
offline deterministic provider; `OpenAICompatibleEmbeddingProvider` can call a
compatible `/embeddings` endpoint, and `SQLiteStore.reindex_vectors()` is
required before switching an existing index to a different model. Model
changes require a full reindex across namespaces; partial reindex is only for
refreshing an already compatible model.

The SQLite adapter is intentionally the local development backend. PostgreSQL
and pgvector deployment is represented by `migrations/001_initial.sql`;
`migrations/002_outbox_claims.sql` supplies `FOR UPDATE SKIP LOCKED` claim,
completion, and retry primitives. The remaining environment-dependent step is
wiring and validating a PostgreSQL repository adapter against a real server.

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
