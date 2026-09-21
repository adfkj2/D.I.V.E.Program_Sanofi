import os
from pathlib import Path
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("DIVE_TEST_POSTGRES_DSN"),
    reason="set DIVE_TEST_POSTGRES_DSN for the opt-in PostgreSQL suite",
)


def _connect():
    psycopg = pytest.importorskip("psycopg")
    return psycopg.connect(os.environ["DIVE_TEST_POSTGRES_DSN"], autocommit=False)


def test_all_migrations_apply_and_are_idempotent():
    from dive_memory.postgres_migrations import apply_migrations
    from psycopg import sql

    root = Path(__file__).parents[3] / "migrations"
    schema = f"dive_migration_test_{uuid.uuid4().hex}"
    with _connect() as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        try:
            first = apply_migrations(connection, root)
            second = apply_migrations(connection, root)
            tables = {row[0] for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                (schema,),
            )}
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            connection.commit()

    assert first
    assert second == []
    assert {
        "events", "outbox", "memories", "memory_transitions", "memory_keys",
        "embedding_generations", "embedding_state", "memory_vectors",
    } <= tables


def test_skip_locked_claims_do_not_double_claim():
    import uuid

    namespace = f"pg-contract-{uuid.uuid4()}"
    event_ids = [f"evt-{uuid.uuid4()}" for _ in range(4)]
    with _connect() as setup:
        for event_id in event_ids:
            setup.execute(
                "INSERT INTO events(id,namespace,event_type,payload,observed_at,idempotency_key) "
                "VALUES (%s,%s,'message','{}',now(),%s)",
                (event_id, namespace, event_id),
            )
            setup.execute("INSERT INTO outbox(event_id) VALUES (%s)", (event_id,))
        setup.commit()

    with _connect() as first, _connect() as second:
        claimed_first = {row[0] for row in first.execute("SELECT * FROM claim_memory_outbox(2,3)")}
        first.commit()
        claimed_second = {row[0] for row in second.execute("SELECT * FROM claim_memory_outbox(2,3)")}
        second.commit()

    assert len(claimed_first) == len(claimed_second) == 2
    assert claimed_first.isdisjoint(claimed_second)

    with _connect() as cleanup:
        cleanup.execute("DELETE FROM outbox WHERE event_id=ANY(%s)", (event_ids,))
        cleanup.execute("DELETE FROM events WHERE id=ANY(%s)", (event_ids,))
        cleanup.commit()


def test_stale_claim_is_recovered_but_exhausted_event_is_not_retried():
    namespace = f"pg-retry-{uuid.uuid4()}"
    stale_event = f"evt-{uuid.uuid4()}"
    exhausted_event = f"evt-{uuid.uuid4()}"
    event_ids = [stale_event, exhausted_event]
    try:
        with _connect() as setup:
            for event_id in event_ids:
                setup.execute(
                    "INSERT INTO events(id,namespace,event_type,payload,observed_at,idempotency_key) "
                    "VALUES (%s,%s,'message','{}',now(),%s)",
                    (event_id, namespace, event_id),
                )
                setup.execute("INSERT INTO outbox(event_id) VALUES (%s)", (event_id,))
            setup.execute(
                "UPDATE outbox SET status='PROCESSING',claimed_at=now()-interval '10 minutes' "
                "WHERE event_id=%s",
                (stale_event,),
            )
            setup.execute(
                "UPDATE outbox SET status='FAILED',attempts=3 WHERE event_id=%s",
                (exhausted_event,),
            )
            setup.commit()

        with _connect() as worker:
            claimed = {row[0] for row in worker.execute("SELECT * FROM claim_memory_outbox(100,3)")}
            state = worker.execute(
                "SELECT status,attempts FROM outbox WHERE event_id=%s", (stale_event,)
            ).fetchone()
            worker.commit()

        assert stale_event in claimed
        assert exhausted_event not in claimed
        assert state == ("PROCESSING", 1)
    finally:
        with _connect() as cleanup:
            cleanup.execute("DELETE FROM outbox WHERE event_id=ANY(%s)", (event_ids,))
            cleanup.execute("DELETE FROM events WHERE id=ANY(%s)", (event_ids,))
            cleanup.commit()
