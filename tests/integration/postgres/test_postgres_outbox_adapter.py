"""Native adapter/service outbox contracts beyond the SQL primitive itself."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from dive_memory.service import MemoryService

pytestmark = pytest.mark.skipif(
    not os.environ.get("DIVE_TEST_POSTGRES_DSN"),
    reason="set DIVE_TEST_POSTGRES_DSN for the opt-in PostgreSQL suite",
)


class FailingExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, text, **kwargs):
        self.calls += 1
        raise RuntimeError("synthetic extraction failure")


def test_postgres_service_pending_events_and_failed_processing_stop_at_retry_limit(
    isolated_postgres_dsn,
):
    from dive_memory.postgres_store import PostgresStore

    store = PostgresStore(isolated_postgres_dsn)
    extractor = FailingExtractor()
    service = MemoryService(store=store, extractor=extractor)
    try:
        deferred = service.ingest(
            "project",
            "A violet sundial was noted.",
            source_type="user",
            observed_at="2026-06-10T09:00:00Z",
            defer=True,
        )
        event_id = deferred["event_id"]
        for attempt in range(1, 4):
            pending = service.pending_events(limit=1)
            assert [event.id for event in pending] == [event_id]
            assert pending[0].namespace == "project"
            assert pending[0].payload["text"] == "A violet sundial was noted."
            assert pending[0].payload["source_type"] == "user"
            # Listing pending work must not claim it or increment an attempt.
            before = store.connection.execute(
                "SELECT status,attempts,claimed_at FROM outbox WHERE event_id=%s",
                (event_id,),
            ).fetchone()
            assert before == {
                "status": "PENDING" if attempt == 1 else "FAILED",
                "attempts": attempt - 1,
                "claimed_at": None,
            }
            result = service.process_pending(limit=1)
            assert len(result) == 1 and result[0]["event_id"] == event_id
            assert result[0]["memory_ids"] == []
            assert result[0]["error"] == "synthetic extraction failure"
            assert result[0]["attempts"] == attempt
            assert result[0]["status"] == "FAILED"
            assert result[0]["retryable"] is (attempt < 3)

        assert extractor.calls == 3
        assert service.pending_events() == []
        assert service.process_pending() == []
        assert store.claim_pending_events() == []
        assert extractor.calls == 3
        assert store.connection.execute(
            "SELECT status,attempts,claimed_at FROM outbox WHERE event_id=%s",
            (event_id,),
        ).fetchone() == {"status": "FAILED", "attempts": 3, "claimed_at": None}
        assert service.list_memories("project") == []
    finally:
        store.purge_namespace("project")
        store.close()


def test_postgres_adapter_claims_skip_locked_work_held_by_another_adapter(
    isolated_postgres_dsn,
):
    from dive_memory.postgres_store import PostgresStore

    first = PostgresStore(isolated_postgres_dsn)
    second = PostgresStore(isolated_postgres_dsn)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        service = MemoryService(store=first)
        event_ids = {
            service.ingest("project", f"Deferred source record {index}.", defer=True)[
                "event_id"
            ]
            for index in range(4)
        }
        assert {event.id for event in service.pending_events()} == event_ids
        # Keep the first worker's row locks uncommitted while a separate
        # adapter/connection claims the rest. Committing first would not test
        # SKIP LOCKED and could conceal a blocking adapter implementation.
        with first.transaction():
            first_claims = first.claim_pending_events(limit=2)
            future = pool.submit(second.claim_pending_events, 2)
            second_claims = future.result(timeout=5)
            first_ids = {event.id for event in first_claims}
            second_ids = {event.id for event in second_claims}
            assert len(first_claims) == len(second_claims) == 2
            assert first_ids.isdisjoint(second_ids)
            assert first_ids | second_ids == event_ids
            assert all(event.namespace == "project" for event in first_claims)
            assert all(event.namespace == "project" for event in second_claims)
        assert first.pending_events() == second.pending_events() == []
        assert first.claim_pending_events() == second.claim_pending_events() == []
        states = first.connection.execute(
            "SELECT status,attempts,claimed_at FROM outbox WHERE event_id=ANY(%s)",
            (list(event_ids),),
        ).fetchall()
        assert len(states) == 4
        assert all(row["status"] == "PROCESSING" for row in states)
        assert all(row["attempts"] == 0 for row in states)
        assert all(row["claimed_at"] is not None for row in states)
    finally:
        # This cleanup occurs after the first transaction exits, including on
        # timeout, so a blocked second worker can finish before connections close.
        pool.shutdown(wait=True)
        first.purge_namespace("project")
        first.close()
        second.close()
