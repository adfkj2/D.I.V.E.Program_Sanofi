# PostgreSQL + pgvector opt-in environment

This environment is for integration tests and reproducible benchmarks. Default
pytest remains offline and does not require Docker or PostgreSQL.

## Start the test database

```powershell
docker run --name dive-memory-phase3-pg `
  -e POSTGRES_USER=dive `
  -e POSTGRES_PASSWORD=dive_test_password `
  -e POSTGRES_DB=dive_test `
  -p 127.0.0.1:55432:5432 `
  -d pgvector/pgvector:pg16
```

Use a local test password only. Do not reuse this command or credential for a
shared or production database.

## Install and run

```powershell
.venv\Scripts\python -m pip install -e ".[postgres,dev]"
$env:DIVE_TEST_POSTGRES_DSN = "postgresql://dive:dive_test_password@127.0.0.1:55432/dive_test?connect_timeout=5"
.venv\Scripts\python -m pytest -q tests\integration\postgres --basetemp .pytest-postgres
.venv\Scripts\python -m dive_memory.postgres_benchmark --memories 10000 --queries 20
```

The integration suite creates isolated schemas where a clean migration history
is required and uses unique namespaces elsewhere. It verifies migration
idempotency, worker claims/recovery, projection rollback, concurrent
idempotency, namespace isolation, temporal transitions, exact pgvector search
and hard purge. The benchmark always creates and drops a unique schema.

## Migration policy

Migrations are forward-only and checksum-verified by
`dive_memory.postgres_migrations`. Re-running them is idempotent. Editing an
already applied migration causes a checksum error; add a new numbered migration
instead. A production rollout still needs backup/restore rehearsal and a
version-specific rollback plan.

## Stop or remove the local container

```powershell
docker stop dive-memory-phase3-pg
docker start dive-memory-phase3-pg
```

Remove the container only when its test data is no longer needed:

```powershell
docker rm dive-memory-phase3-pg
```

## Known limits

- The committed 10K result uses synthetic 96-d vectors, not BGE-M3 or another
  quality model.
- Exact dense-only search is measured; HNSW/IVFFlat are not selected or tested.
- RLS, backups, restore, connection-loss fault injection, concurrent load and
  100K/1M scale remain unverified.
- The PostgreSQL adapter currently covers the measured online ingest,
  retrieve and memory-delete path; replay, reindex, job, event-delete and export
  parity with SQLite remains follow-up work.
- Chinese lexical behavior uses PostgreSQL `simple` FTS and predicate/entity
  signals; parity with SQLite CJK bigrams is not claimed.

On Windows, a Docker Desktop error mentioning inaccessible `dockerInference`
or `engine.sock` is a host runtime failure, not a database migration failure.
Quit Docker Desktop, update/restart it, and clear or rename only the reported
stale runtime socket directory before retrying. Do not reset project files or
the PostgreSQL schema to work around that host error.
