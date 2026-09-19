from pathlib import Path


def test_postgres_reference_migration_covers_runtime_projections():
    sql = (Path(__file__).parents[1] / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    for table in ("events", "outbox", "memories", "memory_vectors", "tombstones", "profiles"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "search_document tsvector" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "<<<<<<<" not in sql
