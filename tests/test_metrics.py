"""Runtime observability contract: /metrics must expose bounded, truthful signals."""

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from dive_memory.application.runtime import MemoryApplication
from dive_memory.observability import RequestMetrics
from dive_memory.web import create_workbench

METRIC_LINE = re.compile(r"^[a-z_]+(\{.*\})? [0-9.eE+-]+$")


def scrape(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    return response.text


def value_of(text, name, labels=""):
    pattern = re.compile(
        rf"^{re.escape(name)}(?:\{{{re.escape(labels)}[^}}]*\}})? ([0-9.]+)$", re.M
    )
    match = pattern.search(text)
    assert match, f"{name} not found in exposition:\n{text}"
    return float(match.group(1))


def test_metrics_exposition_format_and_readiness(tmp_path):
    with TestClient(create_workbench(MemoryApplication(":memory:"))) as client:
        first = scrape(client)
        # Every non-comment line is a well-formed sample; labels are route
        # templates, never raw request paths with ids baked in.
        for line in first.strip().splitlines():
            if not line.startswith("#"):
                assert METRIC_LINE.match(line), line
            assert "/api/sources/mem" not in line and "unmatched" not in line
        assert value_of(first, "dive_readiness", 'component="database"') == 1.0
        assert value_of(first, "dive_readiness", 'component="overall"') == 1.0
        assert value_of(first, "dive_process_uptime_seconds") >= 0.0
        assert value_of(first, "dive_build_info", 'version="0.4.0"') == 1.0

        assert (
            client.post(
                "/api/records",
                json={
                    "project": "p",
                    "key": "budget",
                    "value": "40",
                    "idempotency_key": "m1",
                },
            ).status_code
            == 200
        )
        client.get("/api/sources/does-not-exist", params={"project": "p"})
        client.get("/no/such/route")

        second = scrape(client)
        assert (
            value_of(
                second, "dive_http_requests_total", 'path="/api/records",status="200"'
            )
            >= 1.0
        )
        # Path-parameter routes collapse to their template; misses stay bounded.
        assert (
            value_of(
                second,
                "dive_http_requests_total",
                'path="/api/sources/{memory_id}",status="404"',
            )
            >= 1.0
        )
        assert (
            value_of(
                second, "dive_http_requests_total", 'path="unmatched",status="404"'
            )
            >= 1.0
        )
        assert (
            value_of(
                second,
                "dive_http_request_duration_seconds_count",
                'path="/api/records"',
            )
            >= 1.0
        )


def test_metrics_survive_storage_failure(tmp_path):
    application = MemoryApplication(":memory:")
    with TestClient(create_workbench(application)) as client:
        application.service.store.db.close()
        body = scrape(client)
        assert value_of(body, "dive_readiness", 'component="database"') == 0.0
        assert client.get("/api/readiness").status_code == 503
        # The scrape endpoint itself still responds when the database is gone.
        assert "dive_http_requests_total" in scrape(client)
    application.close()


def test_denied_requests_are_counted_and_cannot_be_cached():
    with TestClient(
        create_workbench(MemoryApplication(), access_token="local-test")
    ) as client:
        for expected, headers in (
            (401, {}),
            (403, {"Origin": "https://example.invalid"}),
        ):
            response = client.get(
                "/api/snapshot?project=private-project", headers=headers
            )
            assert response.status_code == expected
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            body = scrape(client)
            assert (
                value_of(
                    body,
                    "dive_http_requests_total",
                    f'path="unmatched",status="{expected}"',
                )
                == 1
            )
            assert "private-project" not in body and "local-test" not in body


def test_probe_exception_is_reported_as_unavailable(monkeypatch):
    application = MemoryApplication()
    with TestClient(create_workbench(application)) as client:

        def failed_probe():
            raise sqlite3.OperationalError("injected storage failure")

        monkeypatch.setattr(application, "readiness", failed_probe)
        assert value_of(scrape(client), "dive_readiness", 'component="overall"') == 0


def test_concurrent_scrapes_have_consistent_counters_and_summary_type():
    metrics = RequestMetrics()

    def render():
        return metrics.render(ready=True, database_ready=True, mode="evidence_only")

    def write():
        for _ in range(250):
            metrics.record("/api/answer", 200, 0.125)

    with ThreadPoolExecutor(max_workers=4) as pool:
        writers = [pool.submit(write) for _ in range(3)]
        for _ in range(30):
            body = render()
            if 'path="/api/answer"' in body:
                count = value_of(
                    body,
                    "dive_http_request_duration_seconds_count",
                    'path="/api/answer"',
                )
                total = value_of(
                    body, "dive_http_request_duration_seconds_sum", 'path="/api/answer"'
                )
                assert total == count * 0.125
        for writer in writers:
            writer.result()
    body = render()
    assert "# TYPE dive_http_request_duration_seconds summary" in body
    assert (
        value_of(body, "dive_http_requests_total", 'path="/api/answer",status="200"')
        == 750
    )


def test_uptime_uses_injected_monotonic_clock_and_labels_are_escaped():
    ticks = iter([100.0, 102.5])
    metrics = RequestMetrics(clock=lambda: next(ticks))
    body = metrics.render(ready=False, database_ready=False, mode='quote"\nslash\\')
    assert value_of(body, "dive_process_uptime_seconds") == 2.5
    assert 'mode="quote\\"\\nslash\\\\"' in body
