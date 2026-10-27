"""Completion termination evidence survives transport and cost recording."""

import importlib.util
import io
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from dive_memory.application.oracle_run import RequestLedger
from dive_memory.reading.providers import (
    ChatTransportError,
    OpenAICompatibleChatClient,
)
from dive_memory.reading.reader import ChatCompletion


def envelope(content="{}", finish_reason="stop", **usage):
    return {
        "model": "resolved-fixture",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": content,
                    "reasoning_content": "private reasoning must never be persisted",
                },
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 8192, **usage},
    }


def client_for(monkeypatch, payload):
    calls = []

    def respond(request, **kwargs):
        calls.append((json.loads(request.data), kwargs))
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", respond)
    return OpenAICompatibleChatClient("https://example.invalid/chat", "test-key"), calls


def complete(client):
    return client.complete(
        model="fixture",
        messages=[{"role": "user", "content": "Return a JSON object."}],
        temperature=0,
        max_tokens=8192,
        timeout_seconds=5,
        response_format={"type": "json_object"},
    )


@pytest.mark.parametrize("content", ["", None, '{"unfinished":'])
def test_length_completion_retains_empty_or_partial_text_and_known_usage(
    monkeypatch, content
):
    client, calls = client_for(
        monkeypatch,
        envelope(
            content,
            "length",
            completion_tokens_details={"reasoning_tokens": 8100},
        ),
    )
    response = complete(client)
    assert response.text == (content or "")
    assert response.finish_reason == "length"
    assert response.reasoning_tokens == 8100
    assert (response.input_tokens, response.output_tokens) == (11, 8192)
    assert len(calls) == 1
    assert calls[0][0]["max_tokens"] == 8192
    assert "reasoning_content" not in asdict(response)
    assert "private reasoning" not in json.dumps(asdict(response))


def test_normal_completion_and_older_provider_without_optional_metadata(monkeypatch):
    client, _ = client_for(monkeypatch, envelope(completion_tokens_details={}))
    response = complete(client)
    assert response.finish_reason == "stop" and response.reasoning_tokens is None
    payload = envelope()
    del payload["choices"][0]["finish_reason"]
    client, _ = client_for(monkeypatch, payload)
    response = complete(client)
    assert response.finish_reason is None and response.reasoning_tokens is None
    assert ChatCompletion("{}", "legacy").finish_reason is None


@pytest.mark.parametrize("finish_reason", [None, "", True, 7, [], {}])
def test_null_content_without_explicit_termination_retains_legacy_error(
    monkeypatch, finish_reason
):
    client, calls = client_for(monkeypatch, envelope(None, finish_reason))
    with pytest.raises(ChatTransportError, match="no content"):
        complete(client)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reason", ["stop", "content_filter", "tool_calls", "insufficient_system_resource"]
)
def test_null_content_with_explicit_termination_retains_known_usage(
    monkeypatch, reason
):
    client, calls = client_for(monkeypatch, envelope(None, reason))
    response = complete(client)
    assert response.text == "" and response.finish_reason == reason
    assert (response.input_tokens, response.output_tokens) == (11, 8192)
    assert len(calls) == 1


def test_missing_content_field_is_not_a_completed_null_message(monkeypatch):
    payload = envelope(None, "length")
    del payload["choices"][0]["message"]["content"]
    client, _ = client_for(monkeypatch, payload)
    with pytest.raises(ChatTransportError, match="missing"):
        complete(client)


@pytest.mark.parametrize("finish_reason", [None, True, 7, [], {}])
def test_invalid_finish_reason_remains_unknown(monkeypatch, finish_reason):
    client, _ = client_for(monkeypatch, envelope("{}", finish_reason))
    assert complete(client).finish_reason is None


@pytest.mark.parametrize("reason", ["content_filter", "tool_calls", "vendor-stop"])
def test_string_finish_reason_is_preserved_without_inventing_success(
    monkeypatch, reason
):
    client, _ = client_for(monkeypatch, envelope("{}", reason))
    assert complete(client).finish_reason == reason


@pytest.mark.parametrize("value", [None, True, -1, 2.0, "2", [], {}])
def test_reasoning_usage_accepts_only_nonnegative_integer_tokens(monkeypatch, value):
    client, _ = client_for(
        monkeypatch, envelope(completion_tokens_details={"reasoning_tokens": value})
    )
    response = complete(client)
    assert response.reasoning_tokens is None
    assert response.output_tokens == 8192


@pytest.mark.parametrize("value", [0, 14])
def test_zero_and_positive_reasoning_usage_are_preserved(monkeypatch, value):
    client, _ = client_for(
        monkeypatch, envelope(completion_tokens_details={"reasoning_tokens": value})
    )
    assert complete(client).reasoning_tokens == value


@pytest.mark.parametrize("details", [None, [], "tokens", 7, True])
def test_malformed_optional_details_do_not_discard_known_total_usage(
    monkeypatch, details
):
    client, _ = client_for(monkeypatch, envelope(completion_tokens_details=details))
    response = complete(client)
    assert response.reasoning_tokens is None
    assert (response.input_tokens, response.output_tokens) == (11, 8192)


@pytest.mark.parametrize("usage", [None, [], [1], "tokens", 7, True])
def test_non_object_usage_is_unknown_without_fabrication(monkeypatch, usage):
    payload = envelope()
    payload["usage"] = usage
    client, calls = client_for(monkeypatch, payload)
    response = complete(client)
    assert response.input_tokens is None and response.output_tokens is None
    assert response.reasoning_tokens is None
    assert response.finish_reason == "stop" and len(calls) == 1


@pytest.mark.parametrize("value", [True, -1, 2.0, 2.5, "2", float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens"])
def test_malformed_total_usage_never_becomes_a_known_charge(monkeypatch, value, field):
    client, _ = client_for(monkeypatch, envelope(**{field: value}))
    response = complete(client)
    observed = (
        response.input_tokens if field == "prompt_tokens" else response.output_tokens
    )
    assert observed is None


def recorder(directory):
    spec = importlib.util.spec_from_file_location(
        "completion_recorder_fixture",
        Path(__file__).parents[1] / "tools/run_reader_challenge.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RequestRecorder(directory)


def test_termination_metadata_is_durable_and_reasoning_is_not_double_charged(
    monkeypatch, tmp_path
):
    client, _ = client_for(
        monkeypatch,
        envelope(None, "length", completion_tokens_details={"reasoning_tokens": 8192}),
    )
    ledger = RequestLedger(tmp_path / "ledger.json", 1, 20000)
    response = complete(ledger.wrap(recorder(tmp_path).wrap(client)))
    assert response.text == ""
    assert ledger.peak_cost_cny() == Decimal("0.065558")
    ledger_row = json.loads(ledger.path.read_text())["requests"][0]
    recorded = json.loads((tmp_path / "request-0001.json").read_text())
    for row in (ledger_row, recorded):
        assert row["state"] == "completed"
        assert row["finish_reason"] == "length" and row["reasoning_tokens"] == 8192
        assert "private reasoning" not in json.dumps(row)
        assert "test-key" not in json.dumps(row)
    assert RequestLedger(ledger.path, 2, 40000).peak_cost_cny() == Decimal("0.065558")


def test_recorders_keep_legacy_client_responses_compatible(tmp_path):
    response = SimpleNamespace(
        text="{}", model="legacy", input_tokens=3, output_tokens=2
    )
    client = SimpleNamespace(
        endpoint="https://example.invalid",
        extra_body={},
        complete=lambda **kwargs: response,
    )
    ledger = RequestLedger(tmp_path / "ledger.json", 1, 20000)
    assert complete(ledger.wrap(recorder(tmp_path).wrap(client))) is response
    for row in (
        ledger.rows[0],
        json.loads((tmp_path / "request-0001.json").read_text()),
    ):
        assert row["finish_reason"] is None and row["reasoning_tokens"] is None
    assert ledger.peak_cost_cny() == Decimal("0.000022")
    legacy = json.loads(ledger.path.read_text())
    legacy["requests"][0].pop("finish_reason")
    legacy["requests"][0].pop("reasoning_tokens")
    ledger.path.write_text(json.dumps(legacy))
    original = ledger.path.read_bytes()
    reopened = RequestLedger(ledger.path, 2, 40000)
    assert reopened.peak_cost_cny() == Decimal("0.000022")
    assert ledger.path.read_bytes() == original
