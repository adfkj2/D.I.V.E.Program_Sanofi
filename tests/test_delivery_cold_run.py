import json
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


def test_cold_plan_requires_no_key_or_historical_results(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DIVE_READER_API_KEY", raising=False)
    output = tmp_path / "fresh"
    assert main(["--dataset", "not-downloaded.json", "--output", str(output)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "PLAN_ONLY" and not output.exists()
    assert r40_pipeline().semantic_retrieval_only


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
