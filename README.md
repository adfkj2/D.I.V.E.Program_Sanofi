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

The local MVP now covers event sourcing, an outbox worker, selective and
deferred extraction, provenance, temporal supersession, profiles, entities and
relations, hybrid retrieval, context packing, consolidation, decay, deletion
propagation, export, user memory controls, five evaluation baselines, and a
smoke benchmark:

```powershell
$env:PYTHONPATH = "src"
python -m dive_memory.smoke
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
required before switching an existing index to a different model.

The SQLite adapter is intentionally the local development backend. PostgreSQL
and pgvector deployment is represented by `migrations/001_initial.sql`, which
now includes outbox claims, vector metadata, tombstones, and PostgreSQL FTS.
The remaining production step is wiring a PostgreSQL repository adapter around
the same service contract.
