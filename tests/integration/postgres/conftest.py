"""Each store contract owns a migrated schema, including all worker connections."""

import os
from pathlib import Path
import uuid

import pytest


@pytest.fixture
def isolated_postgres_dsn():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from dive_memory.postgres_migrations import apply_migrations

    schema = f"dive_contract_test_{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["DIVE_TEST_POSTGRES_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(
                os.environ["DIVE_TEST_POSTGRES_DSN"],
                options=f"-c search_path={schema},public",
            )
            with psycopg.connect(dsn, autocommit=False) as connection:
                apply_migrations(connection, Path(__file__).parents[3] / "migrations")
            yield dsn
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
