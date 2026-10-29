"""Read-only exit and usage evidence; nothing here authorizes or resumes work."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict, cast


class EvidenceError(ValueError):
    """Durable evidence is missing, ambiguous or inconsistent with its contract."""


class LedgerEvidence(TypedDict):
    ledger_sha256: str
    attempted_calls: int
    completed_calls: int
    unknown_or_failed_calls: int
    known_input_tokens: int
    known_output_tokens: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    known_peak_cost_cny: str
    unknown_full_reservation_cny: str | None
    conservative_peak_cost_cny: str | None
    actual_invoice_cny: None
    must_stop: bool
    resume_authorized: bool


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _price(value: object) -> Decimal:
    if not isinstance(value, str):
        raise EvidenceError("Invalid frozen currency rate")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise EvidenceError("Invalid frozen currency rate") from None
    if not result.is_finite() or result <= 0:
        raise EvidenceError("Invalid frozen currency rate")
    return result


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("Duplicate evidence key")
        result[key] = value
    return result


def inspect_ledger(path: Path, *, expected_rates: Mapping[str, str]) -> LedgerEvidence:
    """Keep unknown reservations without reopening a ledger for execution.

    An unpriced uncertain row makes the total unknown, never zero. Completed
    usage is a peak estimate; actual billing and permission to retry stay unknown
    and false respectively. Original ledger bytes are never changed.
    """
    raw = path.read_bytes()
    try:
        saved = json.loads(raw, object_pairs_hook=_unique)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise EvidenceError("Invalid durable ledger JSON") from None
    if not isinstance(saved, dict):
        raise EvidenceError("Invalid durable ledger object")
    rates = saved.get("peak_rates_cny_per_million")
    if (
        not isinstance(rates, dict)
        or set(rates) != {"input", "output"}
        or rates != dict(expected_rates)
    ):
        raise EvidenceError("Ledger rates differ from the frozen contract")
    input_rate, output_rate = _price(rates["input"]), _price(rates["output"])
    rows = saved.get("requests")
    if not isinstance(rows, list):
        raise EvidenceError("Missing durable request rows")
    known = Decimal(0)
    reserved = Decimal(0)
    missing_reservation = False
    complete = input_tokens = output_tokens = 0
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceError("Invalid durable request row")
        if row.get("state") == "completed" and all(
            _integer(row.get(key)) for key in ("input_tokens", "output_tokens")
        ):
            complete += 1
            input_tokens += row["input_tokens"]
            output_tokens += row["output_tokens"]
            known += (
                input_rate * row["input_tokens"] + output_rate * row["output_tokens"]
            ) / 1_000_000
        elif (
            all(
                _integer(row.get(key))
                for key in ("reserved_input_tokens", "reserved_output_tokens")
            )
            and row["reserved_input_tokens"] + row["reserved_output_tokens"] > 0
        ):
            reserved += (
                input_rate * row["reserved_input_tokens"]
                + output_rate * row["reserved_output_tokens"]
            ) / 1_000_000
        elif _integer(row.get("reserved_tokens")) and row["reserved_tokens"] > 0:
            # Historical unsplit reservations retain the higher peak rate.
            reserved += (
                max(input_rate, output_rate) * row["reserved_tokens"] / 1_000_000
            )
        else:
            missing_reservation = True
    unknown = len(rows) - complete
    return {
        "ledger_sha256": hashlib.sha256(raw).hexdigest(),
        "attempted_calls": len(rows),
        "completed_calls": complete,
        "unknown_or_failed_calls": unknown,
        "known_input_tokens": input_tokens,
        "known_output_tokens": output_tokens,
        "total_input_tokens": input_tokens if unknown == 0 else None,
        "total_output_tokens": output_tokens if unknown == 0 else None,
        "known_peak_cost_cny": str(known),
        "unknown_full_reservation_cny": None if missing_reservation else str(reserved),
        "conservative_peak_cost_cny": None
        if missing_reservation
        else str(known + reserved),
        "actual_invoice_cny": None,
        "must_stop": unknown > 0 or missing_reservation,
        "resume_authorized": False,
    }


def execution_status(
    *,
    external: Mapping[str, object] | None,
    terminal: Mapping[str, object] | None,
    summary: Mapping[str, object] | None,
    ledgers: Sequence[Mapping[str, object]],
    selected: int,
) -> str:
    """Check necessary Oracle execution evidence, never semantic correctness.

    The caller must establish the external observer's task/process identity and
    current start before normalizing these fields. Complete execution still
    requires separate frozen-artifact, original-judge and semantic gate checks.
    """
    if type(selected) is not int or selected <= 0:
        raise EvidenceError("Invalid selected denominator")
    if not isinstance(external, Mapping) or not all(
        external.get(key) is True
        for key in ("identity_verified", "current_start_verified", "stopped")
    ):
        return "UNKNOWN_EXTERNAL_EXIT"
    code = external.get("exit_code")
    if type(code) is not int:
        return "UNKNOWN_EXTERNAL_EXIT"
    if code != 0:
        return "FAILED_EXTERNAL_EXIT"
    if not isinstance(terminal, Mapping):
        return "MISSING_WORKER_TERMINAL"
    if (
        terminal.get("status") != "COMPLETED"
        or type(terminal.get("exit_code")) is not int
        or terminal["exit_code"] != 0
    ):
        return "FAILED_WORKER_TERMINAL"
    if not ledgers or any(
        item.get("must_stop") is not False
        or not _integer(item.get("attempted_calls"))
        or cast(int, item.get("attempted_calls")) == 0
        or type(item.get("completed_calls")) is not int
        or item["completed_calls"] != item["attempted_calls"]
        or type(item.get("unknown_or_failed_calls")) is not int
        or item["unknown_or_failed_calls"] != 0
        or item.get("conservative_peak_cost_cny") is None
        for item in ledgers
    ):
        return "UNKNOWN_OR_FAILED_USAGE"
    if (
        not isinstance(summary, Mapping)
        or summary.get("complete") is not True
        or type(summary.get("generated")) is not int
        or summary["generated"] != selected
        or any(
            type(summary.get(key)) is not int or summary[key] != selected
            for key in ("selected", "judged")
        )
        or type(summary.get("unjudged")) is not int
        or summary["unjudged"] != 0
        or any(
            not _integer(summary.get(key)) for key in ("correct", "wrong", "unknown")
        )
        or sum(cast(int, summary[key]) for key in ("correct", "wrong", "unknown"))
        != selected
        or type(summary.get("invalid")) is not int
        or summary["invalid"] != 0
    ):
        return "INCOMPLETE_RESULTS"
    return "EXECUTION_COMPLETE_SEMANTIC_GATE_STILL_REQUIRED"
