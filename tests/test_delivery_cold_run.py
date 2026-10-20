import json
from decimal import Decimal
from typing import ClassVar

import pytest

from dive_memory.application.oracle_run import (
    RequestLedger,
    RunStopped,
    main,
    r40_pipeline,
)
from dive_memory.application.readers import create_reader
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.oracle.reader import ChatCompletion


def test_currency_cap_reserves_before_network_and_counts_uncertain_usage(tmp_path):
    class Client:
        endpoint = "https://example.invalid"
        extra_body: ClassVar[dict] = {}
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            return ChatCompletion("ok", "fixture", input_tokens=10, output_tokens=2)

    client = Client()
    ledger = RequestLedger(tmp_path / "requests.json", 10, 100000, Decimal("0.001"))
    with pytest.raises(RunStopped, match="Currency"):
        ledger.wrap(client).complete(
            messages=[{"role": "user", "content": "hello"}], max_tokens=100
        )
    assert client.calls == 0 and not ledger.rows
    ledger.rows = [{"state": "pending", "reserved_tokens": 1000}]
    assert ledger.peak_cost_cny() == Decimal("0.008")


def test_model_price_is_used_for_reservation_and_persisted_on_resume(tmp_path):
    from dive_memory.application.oracle_run import TokenPrices

    class Client:
        endpoint = "https://example.invalid"
        extra_body: ClassVar[dict] = {}
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            return ChatCompletion("ok", "fixture", input_tokens=100, output_tokens=50)

    prices = TokenPrices(Decimal("9"), Decimal("27"))
    client = Client()
    path = tmp_path / "ledger.json"
    ledger = RequestLedger(path, 10, 100000, Decimal("0.005"), prices=prices)
    request = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
    with pytest.raises(RunStopped, match="Currency"):
        ledger.wrap(client).complete(**request)
    assert client.calls == 0
    ledger = RequestLedger(path, 10, 100000, Decimal("1"), prices=prices)
    ledger.wrap(client).complete(**request)
    assert ledger.peak_cost_cny() == Decimal("0.00225")
    saved = json.loads(path.read_text())
    assert saved["peak_rates_cny_per_million"] == {"input": "9", "output": "27"}
    assert saved["max_cost_cny"] == "1"
    assert RequestLedger(path, 10, 100000, prices=prices).peak_cost_cny() == Decimal(
        "0.00225"
    )
    with pytest.raises(RunStopped, match="rates"):
        RequestLedger(path, 10, 100000)


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", "Infinity"])
def test_invalid_price_rejected_before_any_request(bad):
    from dive_memory.application.oracle_run import TokenPrices

    with pytest.raises(ValueError, match="positive"):
        TokenPrices(Decimal(bad), Decimal("8"))
    with pytest.raises(ValueError, match="positive"):
        TokenPrices(Decimal("2"), Decimal(bad))


def test_uncertain_pro_usage_reserves_at_higher_price(tmp_path):
    from dive_memory.application.oracle_run import TokenPrices

    ledger = RequestLedger(
        tmp_path / "ledger.json",
        10,
        100000,
        prices=TokenPrices(Decimal("9"), Decimal("27")),
    )
    ledger.rows = [{"state": "pending", "reserved_tokens": 1000}]
    assert ledger.peak_cost_cny() == Decimal("0.027")


def test_input_and_output_reservations_use_their_own_peak_rates(tmp_path):
    from dive_memory.application.oracle_run import TokenPrices

    class Client:
        endpoint = "https://example.invalid"
        extra_body: ClassVar[dict] = {}

        def complete(self, **kwargs):
            raise TimeoutError("unknown remote completion")

    request = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
    reserved_input = (
        len(json.dumps(request["messages"], ensure_ascii=False).encode()) + 1024
    )
    expected = (Decimal("9") * reserved_input + Decimal("27") * 100) / 1000000
    assert expected < Decimal("0.02")
    ledger = RequestLedger(
        tmp_path / "ledger.json",
        10,
        100000,
        Decimal("0.02"),
        prices=TokenPrices(Decimal("9"), Decimal("27")),
    )
    with pytest.raises(RunStopped, match="Transport failed"):
        ledger.wrap(Client()).complete(**request)
    row = ledger.rows[0]
    assert row["reserved_input_tokens"] == reserved_input
    assert row["reserved_output_tokens"] == 100
    assert row["reserved_tokens"] == reserved_input + 100
    assert ledger.peak_cost_cny() == expected
    assert ledger.usage_summary()["input_tokens"] is None


def test_old_ledger_cannot_be_repriced_silently(tmp_path):
    from dive_memory.application.oracle_run import TokenPrices

    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"requests": []}))
    assert RequestLedger(path, 10, 100000).peak_cost_cny() == 0
    with pytest.raises(RunStopped, match="rates"):
        RequestLedger(path, 10, 100000, prices=TokenPrices(Decimal("9"), Decimal("27")))


def test_model_and_prices_are_frozen_in_public_plan(capsys):
    main(["--dataset", "missing", "--output", "unused", "--model", "deepseek-v4-pro"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["model"] == "deepseek-v4-pro"
    assert plan["peak_rates_cny_per_million"] == {"input": "9", "output": "27"}
    assert plan["pricing_as_of"] == "2026-10-19"


def test_unknown_usage_stops_paid_run_instead_of_silently_continuing(tmp_path):
    class Client:
        endpoint = "https://example.invalid"
        extra_body: ClassVar[dict] = {}

        def complete(self, **kwargs):
            return ChatCompletion("ok", "fixture")

    ledger = RequestLedger(tmp_path / "requests.json", 10, 100000, Decimal("1"))
    with pytest.raises(RunStopped, match="usage"):
        ledger.wrap(Client()).complete(
            messages=[{"role": "user", "content": "hello"}], max_tokens=100
        )
    assert ledger.peak_cost_cny() > 0
    assert ledger.usage_summary()["input_tokens"] is None


def test_cold_plan_requires_no_key_or_historical_results(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DIVE_READER_API_KEY", raising=False)
    output = tmp_path / "fresh"
    assert main(["--dataset", "not-downloaded.json", "--output", str(output)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "PLAN_ONLY" and not output.exists()
    assert plan["reasoning_effort"] == "low"
    assert r40_pipeline().semantic_retrieval_only


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_reasoning_effort_is_explicit_in_plan_and_transport(
    effort, capsys, monkeypatch
):
    from dive_memory.application.settings import application_from_environment

    main(["--dataset", "missing", "--output", "unused", "--reasoning-effort", effort])
    assert json.loads(capsys.readouterr().out)["reasoning_effort"] == effort
    for name, value in {
        "DIVE_MODE": "live",
        "DIVE_READER_ENDPOINT": "https://example.invalid",
        "DIVE_READER_MODEL": "fixture",
        "DIVE_READER_API_KEY": "fixture-not-a-key",
        "DIVE_READER_PROVIDER": "deepseek",
        "DIVE_READER_STRATEGY": "adaptive",
        "DIVE_REASONING_EFFORT": effort,
    }.items():
        monkeypatch.setenv(name, value)
    app = application_from_environment(":memory:")
    try:
        assert app.reader.client.extra_body["reasoning_effort"] == effort
    finally:
        app.close()
    monkeypatch.setenv("DIVE_REASONING_EFFORT", "unsupported")
    with pytest.raises(ValueError, match="DIVE_REASONING_EFFORT"):
        application_from_environment(":memory:")


def test_request_cap_and_failed_ledger_prevent_accidental_retry(tmp_path):
    class Client:
        endpoint = "https://example.invalid"
        extra_body: ClassVar[dict] = {}
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            return ChatCompletion(
                text="ok", model="fixture", input_tokens=5, output_tokens=1
            )

    raw = Client()
    ledger = RequestLedger(tmp_path / "requests.json", 1, 10000)
    wrapped = ledger.wrap(raw)
    request = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}
    wrapped.complete(**request)
    with pytest.raises(RunStopped, match="cap"):
        wrapped.complete(**request)
    assert raw.calls == 1
    assert ledger.usage_summary()["input_tokens"] == 5
    assert ledger.usage_summary()["output_tokens"] == 1
    assert ledger.rows[0]["elapsed_seconds"] >= 0
    ledger.rows[0]["state"] = "pending"
    ledger.save()
    with pytest.raises(RunStopped, match="uncertain"):
        RequestLedger(ledger.path, 10, 10000)


def test_usage_never_reports_partial_totals_as_complete(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json", 10, 10000)
    assert ledger.usage_summary()["input_tokens"] == 0
    ledger.rows = [
        {"state": "completed", "input_tokens": 5, "output_tokens": 2},
        {"state": "failed", "input_tokens": None, "output_tokens": None},
    ]
    summary = ledger.usage_summary()
    assert summary["attempted_calls"] == 2
    assert summary["completed_calls"] == 1
    assert summary["missing_usage_calls"] == 1
    assert (
        summary["input_tokens"]
        is summary["output_tokens"]
        is summary["cost_cny"]
        is None
    )


def test_cold_run_rejects_negative_or_zero_case_limit(tmp_path):
    for limit in ("0", "-1"):
        with pytest.raises(SystemExit):
            main(
                [
                    "--dataset",
                    "missing",
                    "--output",
                    str(tmp_path / "out"),
                    "--max-cases",
                    limit,
                ]
            )


@pytest.mark.parametrize(
    "error,status", [(TimeoutError, "TIMEOUT"), (RuntimeError, "ERROR")]
)
def test_model_failures_remain_distinct_from_abstention(error, status):
    class Client:
        def complete(self, **kwargs):
            raise error("injected transport failure")

    app = MemoryApplication(reader=create_reader(Client(), model="injected-test"))
    try:
        app.record("p", ProjectRecord("budget", "20"), idempotency_key="one")
        result = app.answer("p", "What is the budget?")
        assert result["status"] == status
        assert result["answer"] is None
    finally:
        app.close()
