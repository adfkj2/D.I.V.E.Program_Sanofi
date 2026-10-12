import importlib.util
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from oracle_low_cost_pilot import MeteredClient, PilotStopped
from dive_memory.oracle.providers import ScriptedChatClient


def request(client, text="question"):
    return client.complete(model="test", messages=[{"role": "user", "content": text}],
                           temperature=0, max_tokens=128, timeout_seconds=10,
                           response_format={"type": "json_object"})


def test_cache_and_durable_budget(tmp_path):
    transport = ScriptedChatClient(responses=['{"verdict":"yes"}'], input_tokens_per_call=50, output_tokens_per_call=10)
    client = MeteredClient(transport, tmp_path, max_calls=1)
    first = request(client)
    assert request(client) == first
    resumed = MeteredClient(transport, tmp_path, max_calls=1)
    assert request(resumed) == first
    with pytest.raises(PilotStopped):
        request(resumed, "different")
    assert len(transport.prompts) == 1
    assert resumed.summary()["input_tokens"] == 50


def test_reserve_stops_before_network(tmp_path):
    transport = ScriptedChatClient(responses=["unused"])
    with pytest.raises(PilotStopped):
        request(MeteredClient(transport, tmp_path, max_tokens=100))
    assert not transport.prompts


def test_transport_failure_is_redacted_and_blocks_resume(tmp_path):
    class Failing:
        def complete(self, **kwargs):
            raise ValueError("sensitive credential sentinel")
    with pytest.raises(PilotStopped) as stopped:
        request(MeteredClient(Failing(), tmp_path))
    assert "sentinel" not in str(stopped.value)
    assert "sentinel" not in (tmp_path / "ledger.json").read_text()
    with pytest.raises(PilotStopped):
        MeteredClient(Failing(), tmp_path)


def test_external_exact_cache_reuse_never_invokes_balance_hook(tmp_path):
    old = MeteredClient(ScriptedChatClient(responses=["cached"]), tmp_path / "old")
    expected = request(old)
    def stop(_):
        raise AssertionError("must not call hook for cached requests")
    new = MeteredClient(ScriptedChatClient(), tmp_path / "new", max_calls=0,
                        reuse_roots=[tmp_path / "old"], before_request=stop)
    assert request(new) == expected
    assert new.summary()["external_cache_hits_this_process"] == 1
    assert new.summary()["attempts"] == 0


def test_balance_hook_can_stop_before_reserving_or_paying(tmp_path):
    def stop(_):
        raise PilotStopped("limit")
    transport = ScriptedChatClient(responses=["unused"])
    client = MeteredClient(transport, tmp_path, before_request=stop)
    with pytest.raises(PilotStopped):
        request(client)
    assert client.summary()["attempts"] == 0
    assert not transport.prompts


def test_cache_keys_track_actual_transport_settings(tmp_path):
    class Transport:
        endpoint = "https://example.invalid/chat/completions"
        def __init__(self, thinking, answer):
            self.extra_body = {"thinking": {"type": thinking}}
            self.stub = ScriptedChatClient(responses=[answer])
        def complete(self, **kwargs):
            return self.stub.complete(**kwargs)
    off = MeteredClient(Transport("disabled", "off"), tmp_path / "off")
    assert request(off).text == "off"
    on = MeteredClient(Transport("enabled", "on"), tmp_path / "on", reuse_roots=[tmp_path / "off"])
    assert request(on).text == "on"
    assert on.summary()["attempts"] == 1
    same = MeteredClient(Transport("enabled", "unused"), tmp_path / "same", reuse_roots=[tmp_path / "on"])
    assert request(same).text == "on"
    assert same.summary()["attempts"] == 0


def test_currency_bound_checks_next_request_before_reservation(tmp_path):
    from oracle_currency_budget import CurrencyBudget, peak_cost
    transport = ScriptedChatClient(responses=["unused"])
    guard = CurrencyBudget([tmp_path / "ledger.json"], limit="0.00001")
    client = MeteredClient(transport, tmp_path, currency_guard=guard)
    with pytest.raises(PilotStopped):
        request(client)
    assert not transport.prompts and not (tmp_path / "ledger.json").exists()
    assert peak_cost([tmp_path / "ledger.json"])["peak_upper_cny"] == "0"


def test_currency_bound_includes_old_unknown_and_deduplicates_paths(tmp_path):
    import json
    from decimal import Decimal
    from oracle_currency_budget import CurrencyBudget, peak_cost
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"calls": [
        {"status": "completed", "input_tokens": 1000, "output_tokens": 100, "charged_budget": 1100},
        {"status": "pending", "charged_budget": 500},
    ]}), encoding="utf-8")
    assert Decimal(peak_cost([p,p])["peak_upper_cny"]) == Decimal(".0068")
    with pytest.raises(PilotStopped):
        CurrencyBudget([p], limit=".007")(100)
