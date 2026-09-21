from dive_memory.service import MemoryService


def test_trace_records_ranks_scores_latency_and_redacts_query():
    service = MemoryService()
    service.ingest("tenant-secret", "请记住我喜欢绿茶", explicit=True)
    result = service.retrieve("tenant-secret", "我的 secret query 喜欢什么")
    trace = result.trace

    assert trace["schema_version"] == "retrieval-trace-v1"
    assert trace["request_id"].startswith("req_")
    assert "secret query" not in str(trace)
    assert len(trace["query_sha256"]) == 64
    assert all(row["latency_ms"] >= 0 for row in trace["channels"].values())
    candidate = trace["candidates"][0]
    assert candidate["memory_id"] == result.items[0].memory.id
    assert candidate["final_score"] == result.items[0].score
    assert candidate["ranks"]
    assert candidate["raw_scores"]


def test_trace_reports_temporal_exclusion_without_cross_tenant_ids():
    service = MemoryService()
    service.ingest("u1", "我住在成都", explicit=True,
                   occurred_from="2025-01-01T00:00:00+00:00",
                   occurred_to="2025-06-01T00:00:00+00:00")
    service.ingest("u2", "请记住我喜欢敏感项目", explicit=True)

    result = service.retrieve("u1", "成都", as_of="2026-01-01T00:00:00+00:00")

    assert result.trace["exclusions"]["temporal"] == 1
    assert "u2" not in str(result.trace)
    assert "敏感项目" not in str(result.trace)
