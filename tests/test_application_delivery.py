"""User-visible contracts across persistence, the API and installed-style CLI."""

import json

import pytest
from fastapi.testclient import TestClient

from dive_memory.application.demo import load_demo
from dive_memory.application.readers import create_reader
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.cli import main
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.web import create_workbench


@pytest.fixture
def app(tmp_path):
    application = MemoryApplication(str(tmp_path / "project.db"))
    yield application
    application.close()


def add(app, value="20", *, key="budget", token="one", date="2026-01-01", **kwargs):
    return app.record(
        "p",
        ProjectRecord(key, value, "constraint"),
        idempotency_key=token,
        observed_at=date,
        **kwargs,
    )["memory_ids"][0]


def test_current_history_unknown_and_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    first = MemoryApplication(path)
    load_demo(first, "p")
    load_demo(first, "p")
    first.close()
    second = MemoryApplication(path)
    try:
        assert len(second.snapshot("p")["timeline"]) == 7
        assert second.lookup("p", "预算上限")["value"] == "40 元"
        assert second.lookup("p", "预算上限", as_of="2026-10-02")["value"] == "20 元"
        assert second.lookup("p", "当前基线")["value"] == "方案 A"
        assert second.lookup("p", "方案 C 结果")["reason"] == "missing_record"
    finally:
        second.close()


def test_conflicts_require_explicit_update_and_proposal_cannot_replace(app):
    old = add(app)
    add(app, "40", token="two", date="2026-01-02")
    assert app.lookup("p", "budget")["reason"] == "conflict"
    with pytest.raises(ValueError, match="proposal"):
        app.record(
            "p",
            ProjectRecord("budget", "60", "constraint", "proposed"),
            idempotency_key="proposal",
            replaces=old,
            observed_at="2026-01-03",
        )
    with pytest.raises(ValueError, match="later"):
        add(app, "50", token="out-of-order", date="2025-12-31", replaces=old)
    add(app, "40", token="confirmed-correction", date="2026-01-04", replaces=old)
    assert app.lookup("p", "budget")["value"] == "40"
    assert app.lookup("p", "budget", as_of="2026-01-01")["value"] == "20"


def test_idempotency_checks_content_time_and_replacement(app):
    old = add(app)
    assert add(app) == old
    for kwargs in ({"value": "99"}, {"date": "2026-01-02"}, {"replaces": old}):
        with pytest.raises(ValueError, match="idempotency"):
            add(app, **kwargs)
    app.ingest("p", "I live in Paris.", idempotency_key="text", explicit=True)
    with pytest.raises(ValueError, match="idempotency"):
        app.ingest("p", "I live in Rome.", idempotency_key="text", explicit=True)


def test_project_isolation_delete_and_rebuild_never_resurrect(app):
    memory_id = add(app)
    trace = app.answer("p", "budget")
    assert trace["status"] == "EVIDENCE_ONLY" and trace["answer"] is None
    assert trace["evidence"] and trace["estimated_tokens"] <= trace["token_budget"]
    for operation in (app.source, app.forget):
        with pytest.raises(KeyError):
            operation("other", memory_id)
    with pytest.raises(KeyError):
        app.trace("other", trace["trace_id"])
    assert app.snapshot("other")["memories"] == []
    app.forget("p", memory_id)
    app.service.rebuild_projections("p")
    assert app.snapshot("p")["timeline"] == []
    assert app.answer("p", "budget")["evidence"] == []
    with pytest.raises(KeyError):
        app.trace("p", trace["trace_id"])
    with pytest.raises(KeyError):
        app.source("p", memory_id)


def test_update_invalidates_trace_and_preserves_source(app):
    old = add(app)
    trace = app.answer("p", "budget")
    new = add(app, "40", token="two", date="2026-01-02", replaces=old)
    with pytest.raises(KeyError):
        app.trace("p", trace["trace_id"])
    assert "20" in app.source("p", old)["events"][0]["payload"]["text"]
    event_id = app.source("p", old)["events"][0]["id"]
    assert app.inspect_event("p", event_id)["write_decisions"][0]["accepted"]
    with pytest.raises(KeyError):
        app.inspect_event("other", event_id)
    assert app.source("p", new)["memory"]["supersedes_id"] == old


@pytest.mark.parametrize(
    "payload,status",
    [
        (
            {
                "status": "ANSWER",
                "answer": "20",
                "citations": [{"index": 0, "quote": "budget: 20"}],
                "missing": [],
            },
            "ANSWER",
        ),
        (
            {
                "status": "ANSWER",
                "answer": "99",
                "citations": [{"index": 0, "quote": "invented"}],
                "missing": [],
            },
            "INVALID",
        ),
        (
            {
                "status": "ABSTAIN",
                "answer": None,
                "citations": [],
                "missing": ["result"],
            },
            "ABSTAIN",
        ),
    ],
)
def test_real_reader_parser_and_citation_validation(app, payload, status):
    add(app)
    client = ScriptedChatClient(responses=[json.dumps(payload)] * 3)
    app.reader = create_reader(client, model="test")
    result = app.answer("p", "What is the budget?")
    assert result["status"] == status
    assert result["reader"]["calls"] >= 1
    if status == "ANSWER":
        assert (
            result["reader"]["citations"][0]["memory_id"] == result["evidence"][0]["id"]
        )


def test_future_record_not_current_and_projection_failure_recoverable(app, monkeypatch):
    with pytest.raises(ValueError, match="future"):
        add(app, date="2099-01-01")
    assert not app.snapshot("p")["memories"]
    original = app.service._process_event
    monkeypatch.setattr(
        app.service,
        "_process_event",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(RuntimeError):
        add(app, key="latency", token="recover", date="2026-02-01")
    monkeypatch.setattr(app.service, "_process_event", original)
    app.recover()
    assert app.lookup("p", "latency")["value"] == "20"
    assert len(app.lookup("p", "latency")["sources"]) == 1


def test_api_auth_origin_validation_and_cli_share_persistence(tmp_path, capsys):
    path = str(tmp_path / "shared.db")
    assert main(["--db", path, "demo", "--project", "p"]) == 0
    capsys.readouterr()
    with TestClient(
        create_workbench(MemoryApplication(path), access_token="local-test")
    ) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/snapshot", params={"project": "p"}).status_code == 401
        client.headers["Authorization"] = "Bearer local-test"
        assert client.get("/api/readiness").json()["reader"] == "not_configured"
        assert client.get("/api/snapshot", params={"project": "p"}).json()["memories"]
        assert (
            client.post(
                "/api/demo",
                json={"project": "p"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/answer",
                json={"project": "p", "question": "budget", "token_budget": True},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/lookup", params={"project": "p", "key": "预算上限"}
            ).json()["value"]
            == "40 元"
        )
        page = client.get("/")
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "续研" in page.text and client.get("/assets/app.js").status_code == 200
    assert main(["--db", path, "lookup", "--project", "p", "--key", "预算上限"]) == 0
    assert json.loads(capsys.readouterr().out)["value"] == "40 元"


def test_production_application_has_no_experiment_imports():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[1] / "src" / "dive_memory"
    for path in [
        *root.joinpath("application").glob("*.py"),
        root / "web.py",
        root / "cli.py",
    ]:
        source = path.read_text(encoding="utf-8")
        for item in ast.walk(ast.parse(source)):
            if isinstance(item, ast.ImportFrom):
                assert not (item.module or "").startswith(("ops", "audit_tools"))
            elif isinstance(item, ast.Import):
                assert not any(
                    a.name.startswith(("ops", "audit_tools")) for a in item.names
                )
        assert "eval/reports/" not in source
