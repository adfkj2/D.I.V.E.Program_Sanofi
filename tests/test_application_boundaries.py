"""Failure and composition contracts exercised without a model or network call."""

import json
from types import SimpleNamespace

import pytest

from dive_memory.application import settings
from dive_memory.application.oracle_run import RequestLedger, RunStopped
from dive_memory.application.oracle_run import main as oracle_main
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.cli import main
from dive_memory.temporal import expired, overlaps, require_instant, visible_at


@pytest.fixture
def isolated_environment(monkeypatch):
    for key in (
        "DIVE_MODE",
        "DIVE_DB_PATH",
        "DIVE_READER_ENDPOINT",
        "DIVE_READER_MODEL",
        "DIVE_READER_API_KEY",
        "DIVE_READER_STRATEGY",
        "DIVE_READER_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("strategy", ["indexed", "r40", "deliberative-staged"])
def test_live_composition_configures_transports_without_sending(
    strategy, isolated_environment
):
    patch = isolated_environment
    created = []

    def transport(endpoint, key, **options):
        created.append((endpoint, key, options))
        return SimpleNamespace(**options)

    patch.setattr(settings, "OpenAICompatibleChatClient", transport)
    for key, value in {
        "MODE": "live",
        "READER_ENDPOINT": "https://example.invalid/chat",
        "READER_MODEL": "fixture",
        "READER_API_KEY": "test-key",
        "READER_STRATEGY": strategy,
        "READER_PROVIDER": "deepseek",
        "REASONING_EFFORT": "high",
    }.items():
        patch.setenv("DIVE_" + key, value)
    app = settings.application_from_environment(":memory:")
    try:
        assert app.mode == "live_model"
        assert app.readiness()["reader"] == "configured_not_network_probed"
        assert len(created) == (1 if strategy == "indexed" else 2)
        assert created[0][2]["extra_body"] == {"thinking": {"type": "disabled"}}
        if strategy != "indexed":
            assert created[1][2]["extra_body"]["thinking"]["type"] == "enabled"
        if strategy == "deliberative-staged":
            assert app.reader.execution.synthesize.reasoning_effort == "high"
    finally:
        app.close()


@pytest.mark.parametrize("missing", ["ENDPOINT", "MODEL", "API_KEY"])
def test_incomplete_live_configuration_fails_before_transport(
    missing, isolated_environment
):
    patch = isolated_environment
    patch.setenv("DIVE_MODE", "live")
    for suffix in ("ENDPOINT", "MODEL", "API_KEY"):
        if suffix != missing:
            patch.setenv("DIVE_READER_" + suffix, "fixture")
    patch.setattr(
        settings,
        "OpenAICompatibleChatClient",
        lambda *a, **k: pytest.fail("transport created"),
    )
    with pytest.raises(ValueError, match="live mode requires"):
        settings.application_from_environment(":memory:")


@pytest.mark.parametrize(
    "mode,strategy,provider,message",
    [
        ("unknown", "indexed", "fixture", "DIVE_MODE"),
        ("live", "unknown", "fixture", "strategy"),
        ("live", "r40", "fixture", "requires deepseek"),
    ],
)
def test_invalid_composition_is_not_silently_offline(
    mode, strategy, provider, message, isolated_environment
):
    patch = isolated_environment
    for key, value in {
        "MODE": mode,
        "READER_STRATEGY": strategy,
        "READER_PROVIDER": provider,
        "READER_ENDPOINT": "https://example.invalid",
        "READER_MODEL": "fixture",
        "READER_API_KEY": "test-key",
    }.items():
        patch.setenv("DIVE_" + key, value)
    with pytest.raises(ValueError, match=message):
        settings.application_from_environment(":memory:")


def test_offline_mode_never_constructs_model_transport(isolated_environment):
    isolated_environment.setattr(
        settings,
        "OpenAICompatibleChatClient",
        lambda *a, **k: pytest.fail("offline transport"),
    )
    app = settings.application_from_environment(":memory:")
    try:
        assert app.answer("p", "unknown")["status"] == "EVIDENCE_ONLY"
    finally:
        app.close()


def test_failed_request_keeps_reservation_without_provider_message(tmp_path):
    class FailingClient:
        endpoint = "https://example.invalid"

        @property
        def extra_body(self):
            return {}

        def complete(self, **kwargs):
            raise OSError("provider text contains a private token")

    ledger = RequestLedger(tmp_path / "ledger.json", 2, 10000)
    with pytest.raises(RunStopped, match="Transport failed"):
        ledger.wrap(FailingClient()).complete(messages=[], max_tokens=50)
    saved = ledger.path.read_text(encoding="utf-8")
    assert "private token" not in saved
    assert json.loads(saved)["requests"][0]["state"] == "failed"
    assert json.loads(saved)["requests"][0]["reserved_tokens"] > 50
    with pytest.raises(RunStopped, match="uncertain"):
        RequestLedger(ledger.path, 10, 100000)


def test_token_cap_prevents_transport(tmp_path):
    client = SimpleNamespace(complete=lambda **k: pytest.fail("request sent"))
    ledger = RequestLedger(tmp_path / "ledger.json", 10, 1)
    with pytest.raises(RunStopped, match="cap"):
        ledger.wrap(client).complete(messages=[], max_tokens=1)
    assert not ledger.path.exists()


@pytest.mark.parametrize("condition", ["no_caps", "existing_output", "no_key"])
def test_cold_execution_rejects_unsafe_preconditions(
    tmp_path, isolated_environment, condition
):
    output = tmp_path / "run"
    args = ["--dataset", "absent.json", "--output", str(output), "--execute"]
    if condition != "no_caps":
        args += ["--max-calls", "1", "--max-reserved-tokens", "1000"]
    if condition == "existing_output":
        output.mkdir()
    with pytest.raises(SystemExit) as error:
        oracle_main(args)
    assert error.value.code == 2
    assert not output.exists() or not list(output.iterdir())


def test_replacement_rolls_back_partial_projection_then_recovers_after_restart(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "recovery.db")
    app = MemoryApplication(path)
    try:
        first = app.record(
            "p",
            ProjectRecord("budget", "20"),
            idempotency_key="old",
            observed_at="2026-01-01",
        )["memory_ids"][0]
        project = app.service._process_event

        def fail_after_write(*args, **kwargs):
            project(*args, **kwargs)
            raise RuntimeError("injected after projection write")

        monkeypatch.setattr(app.service, "_process_event", fail_after_write)
        with pytest.raises(RuntimeError, match="injected"):
            app.record(
                "p",
                ProjectRecord("budget", "40"),
                idempotency_key="new",
                observed_at="2026-02-01",
                replaces=first,
            )
        assert app.lookup("p", "budget")["value"] == "20"
        assert len(app.snapshot("p")["timeline"]) == 1
    finally:
        app.close()
    restarted = MemoryApplication(path)
    try:
        retry_projection = restarted.service._process_event
        monkeypatch.setattr(
            restarted.service,
            "_process_event",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("still unavailable")),
        )
        failure = restarted.recover()[0]
        assert "still unavailable" in failure["error"]
        assert not failure["memory_ids"]
        assert restarted.lookup("p", "budget")["value"] == "20"
        monkeypatch.setattr(restarted.service, "_process_event", retry_projection)
        assert restarted.recover()[0]["memory_ids"]
        assert restarted.lookup("p", "budget")["value"] == "40"
        assert restarted.lookup("p", "budget", as_of="2026-01-15")["value"] == "20"
        assert restarted.recover() == []
        assert len(restarted.snapshot("p")["timeline"]) == 2
    finally:
        restarted.close()


def test_trace_eviction_and_source_isolation():
    app = MemoryApplication()
    try:
        record = app.record("p", ProjectRecord("budget", "20"), idempotency_key="one")
        old = app.answer("p", "budget")["trace_id"]
        for _ in range(100):
            newest = app.answer("other", "unknown")["trace_id"]
        with pytest.raises(KeyError):
            app.trace("p", old)
        assert app.trace("other", newest)["namespace"] == "other"
        with pytest.raises(KeyError):
            app.trace("p", newest)
        with pytest.raises(KeyError):
            app.source("other", record["memory_ids"][0])
        with pytest.raises(KeyError):
            app.inspect_event("other", record["event_id"])
        assert app.inspect_event("p", record["event_id"])["write_decisions"]
        app.forget("p", record["memory_ids"][0])
        assert app.trace("other", newest)["namespace"] == "other"
    finally:
        app.close()


def test_cli_record_query_erase_and_error_share_real_database(
    tmp_path, isolated_environment, capsys
):
    path = str(tmp_path / "project.db")
    source = tmp_path / "record.json"
    source.write_text(json.dumps({"key": "budget", "value": "20"}), encoding="utf-8")
    prefix = ["--db", path]
    assert (
        main(
            [
                *prefix,
                "record",
                "--project",
                "p",
                str(source),
                "--idempotency-key",
                "one",
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert main([*prefix, "ask", "--project", "p", "budget"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "EVIDENCE_ONLY"
    assert main([*prefix, "forget", "--project", "p", record["memory_ids"][0]]) == 0
    capsys.readouterr()
    assert main([*prefix, "forget", "--project", "p", "unknown"]) == 2
    assert "not found" in capsys.readouterr().err
    assert (
        main(
            [
                *prefix,
                "ingest",
                "--project",
                "p",
                "Please remember I live in Oslo.",
                "--idempotency-key",
                "two",
                "--explicit",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*prefix, "snapshot", "--project", "p"]) == 0
    assert json.loads(capsys.readouterr().out)["namespace"] == "p"
    assert main([*prefix, "recover"]) == 0
    assert json.loads(capsys.readouterr().out)["recovered"] == []


@pytest.mark.parametrize(
    "start,end,query_start,query_end,expected",
    [
        ("2026-01-10", "2026-01-20", "2026-01-01", "2026-01-10", False),
        ("2026-01-01", "2026-01-10", "2026-01-10", "2026-01-20", False),
        ("2026-01-01", "2026-01-11", "2026-01-10", "2026-01-20", True),
        (None, None, "2026-01-10", "2026-01-20", True),
    ],
)
def test_history_intervals_are_half_open(start, end, query_start, query_end, expected):
    assert overlaps(start, end, query_start, query_end) is expected


def test_missing_expiry_stays_visible_but_invalid_public_timestamp_is_rejected():
    assert not expired(None, "2026-02-01")
    assert visible_at("2026-01-01", None, None)
    # Storage tolerates legacy malformed bounds, while user-facing inputs fail closed.
    assert visible_at(None, None, "malformed")
    with pytest.raises(ValueError, match="valid timestamp"):
        require_instant("malformed", "as_of")
