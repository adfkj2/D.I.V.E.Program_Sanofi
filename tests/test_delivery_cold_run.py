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
    ledger.rows[0]["state"] = "pending"
    ledger.save()
    with pytest.raises(RunStopped, match="uncertain"):
        RequestLedger(ledger.path, 10, 10000)


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
