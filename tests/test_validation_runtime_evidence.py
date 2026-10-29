"""Portable failure evidence tests; no Windows scheduler or model service."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from dive_memory.application.oracle_run import RequestLedger, RunStopped

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "validation_runtime_evidence", ROOT / "tools/windows/validation_runtime/evidence.py"
)
evidence = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evidence
spec.loader.exec_module(evidence)
RATES = {"input": "2", "output": "8"}


def ledger_file(tmp_path, rows, *, rates=None):
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "requests": rows,
                "peak_rates_cny_per_million": RATES if rates is None else rates,
            }
        ),
        encoding="utf-8",
    )
    return path


def completed_row(**overrides):
    return {
        "state": "completed",
        "input_tokens": 10,
        "output_tokens": 20,
        "reserved_input_tokens": 100,
        "reserved_output_tokens": 100,
        **overrides,
    }


def test_abrupt_child_retains_original_ledger_reservation(tmp_path):
    """An OS-level exit bypasses finally; the original durable ledger survives."""
    path = tmp_path / "abrupt.json"
    program = """
import os,sys
from decimal import Decimal
from pathlib import Path
from dive_memory.application.oracle_run import RequestLedger
class AbruptTransport:
    endpoint = 'offline-no-network'
    extra_body = None
    def complete(self, **kwargs):
        os._exit(17)
ledger = RequestLedger(Path(sys.argv[1]), 1, 10000, Decimal('1'))
ledger.wrap(AbruptTransport()).complete(messages=[{'role':'user','content':'synthetic'}], max_tokens=100)
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET"))
    }
    environment["PYTHONPATH"] = str(ROOT / "src")
    child = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        timeout=30,
        check=False,
    )
    assert child.returncode == 17
    assert not child.stdout and not child.stderr
    original = path.read_bytes()
    row = json.loads(original)["requests"][0]
    assert row["state"] == "pending"
    result = evidence.inspect_ledger(path, expected_rates=RATES)
    expected = (
        Decimal("2") * row["reserved_input_tokens"]
        + Decimal("8") * row["reserved_output_tokens"]
    ) / 1_000_000
    assert Decimal(result["unknown_full_reservation_cny"]) == expected > 0
    assert result["total_input_tokens"] is None
    assert result["total_output_tokens"] is None
    assert result["must_stop"] and not result["resume_authorized"]
    assert result["ledger_sha256"] == hashlib.sha256(original).hexdigest()
    with pytest.raises(RunStopped):
        RequestLedger(path, 1, 10000, Decimal("1"))
    assert path.read_bytes() == original


def test_transport_exception_does_not_serialize_its_payload(tmp_path):
    marker = os.urandom(32).hex()

    class Transport:
        endpoint = "offline-no-network"
        extra_body = None

        def complete(self, **kwargs):
            raise OSError(marker)

    path = tmp_path / "error.json"
    ledger = RequestLedger(path, 1, 10000, Decimal("1"))
    with pytest.raises(RunStopped):
        ledger.wrap(Transport()).complete(messages=[], max_tokens=100)
    assert marker.encode() not in path.read_bytes()
    result = evidence.inspect_ledger(path, expected_rates=RATES)
    assert result["unknown_or_failed_calls"] == 1
    assert Decimal(result["unknown_full_reservation_cny"]) > 0
    assert result["must_stop"]


def test_completed_usage_estimate_is_not_an_invoice_or_retry_permission(tmp_path):
    result = evidence.inspect_ledger(
        ledger_file(tmp_path, [completed_row()]), expected_rates=RATES
    )
    assert result["known_peak_cost_cny"] == "0.00018"
    assert result["total_input_tokens"] == 10
    assert result["total_output_tokens"] == 20
    assert result["unknown_full_reservation_cny"] == "0"
    assert result["must_stop"] is False
    assert result["actual_invoice_cny"] is None
    assert result["resume_authorized"] is False


@pytest.mark.parametrize("value", [None, True, -1, 2.5, "20"])
def test_bad_completed_usage_retains_the_full_reservation(tmp_path, value):
    result = evidence.inspect_ledger(
        ledger_file(tmp_path, [completed_row(output_tokens=value)]),
        expected_rates=RATES,
    )
    assert result["unknown_or_failed_calls"] == 1
    assert result["unknown_full_reservation_cny"] == "0.001"
    assert result["total_input_tokens"] is None
    assert result["total_output_tokens"] is None
    assert result["must_stop"]


@pytest.mark.parametrize(
    "state", ["pending", "failed", "uncertain_usage", "unexpected"]
)
def test_noncompleted_state_retains_reservation_despite_token_fields(tmp_path, state):
    result = evidence.inspect_ledger(
        ledger_file(tmp_path, [completed_row(state=state)]), expected_rates=RATES
    )
    assert result["completed_calls"] == 0
    assert result["conservative_peak_cost_cny"] == "0.001"
    assert result["must_stop"]


@pytest.mark.parametrize(
    "row",
    [
        {"state": "pending"},
        {"state": "pending", "reserved_tokens": 0},
        {"state": "pending", "reserved_input_tokens": 0, "reserved_output_tokens": 0},
    ],
)
def test_missing_or_zero_uncertain_reservation_is_unknown_not_zero(tmp_path, row):
    result = evidence.inspect_ledger(ledger_file(tmp_path, [row]), expected_rates=RATES)
    assert result["conservative_peak_cost_cny"] is None
    assert result["unknown_full_reservation_cny"] is None
    assert result["must_stop"]


def test_legacy_unsplit_uncertainty_keeps_the_higher_rate(tmp_path):
    result = evidence.inspect_ledger(
        ledger_file(tmp_path, [{"state": "failed", "reserved_tokens": 100}]),
        expected_rates=RATES,
    )
    assert result["conservative_peak_cost_cny"] == "0.0008"
    assert result["must_stop"]


def test_mixed_rows_keep_known_partial_usage_separate(tmp_path):
    result = evidence.inspect_ledger(
        ledger_file(
            tmp_path, [completed_row(), {"state": "pending", "reserved_tokens": 100}]
        ),
        expected_rates=RATES,
    )
    assert result["known_input_tokens"] == 10
    assert result["known_output_tokens"] == 20
    assert result["known_peak_cost_cny"] == "0.00018"
    assert result["conservative_peak_cost_cny"] == "0.00098"
    assert result["total_input_tokens"] is None


@pytest.mark.parametrize("rate", ["0", "-1", "NaN", "Infinity", "bad", True])
def test_invalid_frozen_rates_fail_closed(tmp_path, rate):
    rates = {"input": rate, "output": "8"}
    with pytest.raises(evidence.EvidenceError):
        evidence.inspect_ledger(
            ledger_file(tmp_path, [], rates=rates), expected_rates=rates
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff",
        b"[]",
        b'{"requests": [], "requests": []}',
        b'{"requests": [], "peak_rates_cny_per_million": {"input": "2", "output": "8", "output": "8"}}',
        b'{"requests": {}, "peak_rates_cny_per_million": {"input": "2", "output": "8"}}',
        b'{"requests": [null], "peak_rates_cny_per_million": {"input": "2", "output": "8"}}',
    ],
)
def test_malformed_ledger_cannot_be_treated_as_zero_usage(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(evidence.EvidenceError):
        evidence.inspect_ledger(path, expected_rates=RATES)
    assert path.read_bytes() == raw


def test_changed_rates_are_rejected(tmp_path):
    with pytest.raises(evidence.EvidenceError):
        evidence.inspect_ledger(
            ledger_file(tmp_path, [], rates={"input": "1", "output": "8"}),
            expected_rates=RATES,
        )


@pytest.fixture
def execution(tmp_path):
    return {
        "external": {
            "identity_verified": True,
            "current_start_verified": True,
            "stopped": True,
            "exit_code": 0,
        },
        "terminal": {"status": "COMPLETED", "exit_code": 0},
        "summary": {
            "complete": True,
            "generated": 500,
            "selected": 500,
            "judged": 500,
            "unjudged": 0,
            "invalid": 0,
            "correct": 435,
            "wrong": 57,
            "unknown": 8,
        },
        "ledgers": [
            evidence.inspect_ledger(
                ledger_file(tmp_path, [completed_row()]), expected_rates=RATES
            )
        ],
        "selected": 500,
    }


def test_complete_execution_still_requires_a_separate_semantic_gate(execution):
    assert (
        evidence.execution_status(**execution)
        == "EXECUTION_COMPLETE_SEMANTIC_GATE_STILL_REQUIRED"
    )


@pytest.mark.parametrize("code", [17, -9, 3221225477])
def test_external_failure_is_not_hidden_by_a_successful_worker_file(execution, code):
    execution["external"]["exit_code"] = code
    assert evidence.execution_status(**execution) == "FAILED_EXTERNAL_EXIT"


@pytest.mark.parametrize(
    "field", ["identity_verified", "current_start_verified", "stopped"]
)
def test_external_identity_and_current_terminal_start_must_be_proven(execution, field):
    execution["external"][field] = False
    assert evidence.execution_status(**execution) == "UNKNOWN_EXTERNAL_EXIT"


@pytest.mark.parametrize("code", [None, False, "0"])
def test_absent_or_malformed_exit_code_is_not_success(execution, code):
    execution["external"]["exit_code"] = code
    assert evidence.execution_status(**execution) == "UNKNOWN_EXTERNAL_EXIT"


def test_zero_external_exit_requires_the_worker_terminal(execution):
    execution["terminal"] = None
    assert evidence.execution_status(**execution) == "MISSING_WORKER_TERMINAL"


@pytest.mark.parametrize(
    "terminal",
    [
        {"status": "RUNNING", "exit_code": None},
        {"status": "COMPLETED", "exit_code": False},
    ],
)
def test_stale_or_malformed_worker_terminal_is_not_success(execution, terminal):
    execution["terminal"] = terminal
    assert evidence.execution_status(**execution) == "FAILED_WORKER_TERMINAL"


@pytest.mark.parametrize(
    "field,value",
    [
        ("generated", 499),
        ("selected", 499),
        ("judged", 499),
        ("unjudged", 1),
        ("invalid", 1),
        ("correct", 434),
        ("wrong", True),
        ("complete", False),
    ],
)
def test_incomplete_results_cannot_pass_execution_gate(execution, field, value):
    execution["summary"][field] = value
    assert evidence.execution_status(**execution) == "INCOMPLETE_RESULTS"


def test_pending_usage_blocks_despite_known_zero_exit(execution, tmp_path):
    execution["ledgers"] = [
        evidence.inspect_ledger(
            ledger_file(tmp_path, [{"state": "pending", "reserved_tokens": 100}]),
            expected_rates=RATES,
        )
    ]
    assert evidence.execution_status(**execution) == "UNKNOWN_OR_FAILED_USAGE"


@pytest.mark.parametrize("empty", [[], [{"must_stop": False}]])
def test_missing_or_empty_usage_is_not_a_complete_paid_run(execution, empty):
    execution["ledgers"] = empty
    assert evidence.execution_status(**execution) == "UNKNOWN_OR_FAILED_USAGE"


@pytest.mark.parametrize("selected", [True, 0, -1])
def test_invalid_denominator_is_rejected(execution, selected):
    execution["selected"] = selected
    with pytest.raises(evidence.EvidenceError):
        evidence.execution_status(**execution)
