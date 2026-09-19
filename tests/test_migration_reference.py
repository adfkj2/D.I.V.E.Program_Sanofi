from pathlib import Path


def test_postgres_reference_migration_covers_runtime_projections():
    sql = (Path(__file__).parents[1] / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    for table in ("events", "outbox", "memories", "memory_versions", "memory_vectors", "tombstones",
                  "profiles", "memory_modes", "jobs", "write_decisions"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "search_document tsvector" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "attempts integer" in sql
    assert "extractor_version text" in sql
    assert "<<<<<<<" not in sql


def test_postgres_outbox_claim_uses_skip_locked_and_retry_metadata():
    sql = (Path(__file__).parents[1] / "migrations" / "002_outbox_claims.sql").read_text(encoding="utf-8")
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "attempts = attempts + 1" in sql
    assert "status = 'DONE'" in sql


def test_postgres_tombstone_upgrade_adds_tenant_scope():
    sql = (Path(__file__).parents[1] / "migrations" / "003_tombstone_namespace.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS namespace" in sql
    assert "tombstones_scope_deleted_idx" in sql
    assert "t.namespace IS NULL" in sql
