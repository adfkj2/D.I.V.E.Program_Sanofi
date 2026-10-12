from dataclasses import asdict

from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication


def test_batch_hydration_preserves_all_sources_and_avoids_per_candidate_queries():
    app = MemoryApplication()
    try:
        for i in range(200):
            app.record(
                "p",
                ProjectRecord(f"item-{i}", "adopted result"),
                idempotency_key=f"event-{i}",
            )
        app.record(
            "p",
            ProjectRecord("item-0", "adopted result"),
            idempotency_key="reinforcement",
        )
        app.record(
            "other",
            ProjectRecord("item-0", "private other value"),
            idempotency_key="other",
        )
        rows = app.service.store.db.execute(
            "SELECT * FROM memories WHERE namespace='p' ORDER BY id"
        ).fetchall()
        reference = {row["id"]: asdict(app.service.store._memory(row)) for row in rows}
        batch = {m.id: asdict(m) for m in app.service.store.memories_for_namespace("p")}
        assert reference == batch
        statements = []
        app.service.store.db.set_trace_callback(statements.append)
        result = app.answer("p", "item-0 adopted result")
        app.service.store.db.set_trace_callback(None)
        provenance_reads = [
            s
            for s in statements
            if s.lstrip().upper().startswith("SELECT") and "memory_sources" in s
        ]
        assert len(provenance_reads) == 1
        assert result["evidence"]
        assert all(m["namespace"] == "p" for m in result["evidence"])
        assert all(
            m["source_event_ids"] == reference[m["id"]]["source_event_ids"]
            for m in result["evidence"]
        )
    finally:
        app.close()
