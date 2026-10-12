"""Failure, reservation and recovery checks without API or benchmark calls."""
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import oracle_premise_ledger_experiment as e


HEADER = {"_header": True, "dataset_sha256": "fixture-dataset", "judge": {"model": "fixture"}}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_verdicts(path, rows, header=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in [header or HEADER, *rows]), encoding="utf-8")


def verdict(q="q1", hypothesis="candidate", **changes):
    row = {"question_id": q, "hypothesis_sha256": sha256(
        ("null" if hypothesis is None else hypothesis).encode()).hexdigest()[:16],
        "correct": True, "raw": "yes", "error": None}
    return {**row, **changes}


def trace(q="q1", *, status="OK", hypothesis="candidate", context="visible evidence"):
    return {"question_id": q, "reader_context_text": context, "hypothesis": hypothesis,
            "reader": {"status": status, "spec": {"prompt_version": "fixture"},
                       "detail": json.dumps({"stages": [{"usage": {"input_tokens": 100}}]})}}


@pytest.mark.parametrize("change", [
    {"hypothesis_sha256": "stale"}, {"correct": 1}, {"correct": None},
    {"error": "timeout"}, {"question_id": "foreign"},
])
def test_untrustworthy_judge_checkpoint_cannot_be_reused(tmp_path, change):
    path = tmp_path / "verdicts.jsonl"
    write_verdicts(path, [verdict(**change)])
    with pytest.raises(ValueError):
        e.load_verdict_checkpoints(path, HEADER, {"q1"}, {"q1": trace()})


@pytest.mark.parametrize("rows,header,records", [
    ([verdict(), verdict()], HEADER, {"q1": trace()}),
    ([verdict()], {**HEADER, "judge": {"model": "different"}}, {"q1": trace()}),
    ([verdict(), HEADER], HEADER, {"q1": trace()}),
    ([verdict()], HEADER, {}),
])
def test_checkpoint_identity_and_uniqueness_are_required(tmp_path, rows, header, records):
    path = tmp_path / "verdicts.jsonl"
    write_verdicts(path, rows, header)
    with pytest.raises(ValueError):
        e.load_verdict_checkpoints(path, HEADER, {"q1"}, records)


def test_null_failed_output_checkpoint_is_reusable_without_becoming_correct(tmp_path):
    path = tmp_path / "verdicts.jsonl"
    row = verdict(hypothesis=None, correct=False, raw="INVALID_OUTPUT_RULE")
    write_verdicts(path, [row])
    saved = e.load_verdict_checkpoints(path, HEADER, {"q1"},
                                     {"q1": trace(status="INVALID", hypothesis=None)})
    assert saved == {"q1": row}


def setup_budget(monkeypatch, tmp_path, *, spent="27", planning="29", limit="30", reserve="1"):
    monkeypatch.setattr(e, "ROOT", tmp_path)
    reference = tmp_path / "r26-verdicts.jsonl"
    write_verdicts(reference, [])
    monkeypatch.setattr(e, "R26_VERDICTS", reference)
    write_json(tmp_path / "eval/reports/oracle-road-to-90-budget.json",
               {"planning_stop_cny": planning, "user_limit_cny": limit,
                "settlement_reserve_cny": reserve})
    monkeypatch.setattr(e, "budget_ledgers", lambda out: [])
    monkeypatch.setattr(e, "peak_cost", lambda paths: {"peak_upper_cny": spent})
    return tmp_path / "candidate"


def test_judge_only_resume_reserves_remaining_judges_and_preserves_old_preflight(monkeypatch, tmp_path):
    out = setup_budget(monkeypatch, tmp_path)
    sources = {q: trace(q) for q in ("q1", "q2")}
    for q, record in sources.items():
        write_json(out / "screen/traces" / e.trace_filename(q), record)
    first = e.preflight(out, sources)
    assert first["cases"] == 0 and first["pending_judge_cases"] == 2
    assert Decimal(first["planning_allowance_cny"]) > 0
    write_verdicts(out / "screen-verdicts.jsonl", [verdict()])
    resumed = e.preflight(out, sources)
    assert resumed["cases"] == 0 and resumed["pending_judge_cases"] == 1
    assert Decimal(resumed["planning_allowance_cny"]) * 2 == Decimal(first["planning_allowance_cny"])
    assert e.read_json(out / "screen-cost-preflight.json") == first
    assert len(list(out.glob("screen-cost-preflight*.json"))) == 2


@pytest.mark.parametrize("spent,planning,expected_stop,expected_fit", [
    ("27", "40", "29", True),
    ("29", "40", "29", False),
    ("28.99622", "29", "29", False),
])
def test_preflight_honors_shared_reserve_and_exact_boundary(monkeypatch, tmp_path,
                                                         spent, planning, expected_stop, expected_fit):
    out = setup_budget(monkeypatch, tmp_path, spent=spent, planning=planning)
    record = trace()
    write_json(out / "screen/traces" / e.trace_filename("q1"), record)
    result = e.preflight(out, {"q1": record})
    assert result["cumulative_stop_cny"] == expected_stop
    assert result["fits_budget"] is expected_fit


def test_full_resume_credits_screen_reader_and_judge_once(monkeypatch, tmp_path):
    out = setup_budget(monkeypatch, tmp_path)
    sources = {q: trace(q) for q in ("q1", "q2")}
    monkeypatch.setattr(e, "_load_run", lambda path: ({}, sources))
    write_json(out / "frozen-plan.json", {"screen_ids": ["q1"]})
    write_json(out / "api-cache/ledger.json", {"calls": [
        {"phase": "reader-screen", "status": "completed", "output_tokens": 300}]})
    write_json(out / "screen/traces" / e.trace_filename("q1"), sources["q1"])
    write_verdicts(out / "screen-verdicts.jsonl", [verdict()])
    result = e.preflight(out, sources, full=True)
    assert result["cases"] == 1 and result["pending_judge_cases"] == 1


@pytest.mark.parametrize("outputs,count,expected_p95,expected_output,expected_repair", [
    ([100] * 80, 80, 100, 512, "0.10"),
    ([700] * 76 + [1500] * 4, 80, 700, 700, "0.10"),
    ([1900] * 100, 80, 1900, 1600, "0.25"),
    ([900] * 200, 80, 900, 900, "1"),
])
def test_full_calibration_retains_envelope_and_enforces_forecast_bounds(tmp_path, outputs, count,
                                                                    expected_p95, expected_output,
                                                                    expected_repair):
    write_json(tmp_path / "frozen-plan.json", {"screen_ids": [str(i) for i in range(count)]})
    calls = [{"phase": "reader-screen", "status": "completed", "output_tokens": n} for n in outputs]
    # Judge and full-stage usage must not alter the pre-registered screen forecast.
    calls.extend({"phase": phase, "status": "completed", "output_tokens": 999999}
                 for phase in ("judge-screen", "reader-full"))
    write_json(tmp_path / "api-cache/ledger.json", {"calls": calls})
    worst = {"cases": 420, "pending_judge_cases": 421, "projected_first_input_tokens": 123456,
             "planning_allowance_cny": "99.00"}
    result = e.calibrated_full_cost(tmp_path, worst)
    assert result["observed_screen_output_p95"] == expected_p95
    assert result["projected_output_per_call"] == expected_output
    assert Decimal(result["projected_repair_rate"]) == Decimal(expected_repair)
    assert result["all_repairs_envelope_cny"] == "99.00"
    assert result["screen_reader_calls"] == len(outputs)
    assert Decimal(result["planning_allowance_cny"]) == Decimal(result["predicted_reader_and_judge_cny"]) * Decimal("1.25")
    assert worst["planning_allowance_cny"] == "99.00"


@pytest.mark.parametrize("rows", [[],
    [{"phase": "reader-screen", "status": "pending", "output_tokens": 123}],
    [{"phase": "reader-screen", "status": "completed", "output_tokens": None}],
    [{"phase": "reader-screen", "status": "completed", "output_tokens": True}],
])
def test_full_calibration_requires_completed_measured_screen_usage(tmp_path, rows):
    write_json(tmp_path / "frozen-plan.json", {"screen_ids": ["q1"]})
    write_json(tmp_path / "api-cache/ledger.json", {"calls": rows})
    with pytest.raises(ValueError, match="Screen usage unavailable"):
        e.calibrated_full_cost(tmp_path, {})


def test_estimate_allows_repair_on_every_case_and_falls_back_for_missing_usage():
    no_usage = trace()
    no_usage["reader"]["detail"] = json.dumps({"stages": [{"usage": {}}]})
    result = e.estimate_cost({"q1": no_usage}, judge_cases=0)
    minimum_output_cost = Decimal(2 * 8 * e.OUTPUT_CAP) / 1000000
    assert result["reader_attempts_per_new_case"] == 2
    assert Decimal(result["predicted_reader_and_judge_cny"]) > minimum_output_cost
    expanded = {"q1": {**no_usage, "reader_context_text": "中" * 10000}}
    more = e.estimate_cost({"q1": no_usage}, judge_cases=0, evidence=expanded)
    assert Decimal(more["planning_allowance_cny"]) > Decimal(result["planning_allowance_cny"])


def test_estimate_accepts_plain_text_failure_detail_without_losing_reservation():
    failed = trace(status="INVALID", hypothesis=None)
    failed["reader"]["detail"] = json.dumps({"stages": [{
        "detail": "Response was not valid JSON", "usage": {"input_tokens": 100}}]})
    result = e.estimate_cost({"q1": failed}, judge_cases=1)
    assert result["cases"] == 1 and result["pending_judge_cases"] == 1
    assert Decimal(result["planning_allowance_cny"]) > 0


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("status", ["ERROR", "TIMEOUT"])
def test_transport_failure_checkpoint_is_preserved_without_resampling(monkeypatch, tmp_path, full, status):
    record = trace(status=status, hypothesis=None)
    screening = tmp_path / "screen/traces" / e.trace_filename("q1")
    write_json(screening, record)
    before = screening.read_bytes()
    reader_factory = Mock(return_value=object())
    generate = Mock(side_effect=AssertionError("must not regenerate a failed checkpoint"))
    monkeypatch.setattr(e, "make_reader", reader_factory)
    monkeypatch.setattr(e, "make_record", generate)
    monkeypatch.setattr(e.previous, "evidence_audit", lambda *args: None)
    monkeypatch.setattr(e, "audit_citations", lambda *args: 0)
    plan = {"all_ids": ["q1"], "screen_ids": ["q1"], "reader_spec": record["reader"]["spec"]}
    with pytest.raises(e.PilotStopped, match="no automatic resampling"):
        e.run_stage(tmp_path, None, {}, {}, {}, plan, {"q1": record}, object(), full=full)
    assert screening.read_bytes() == before
    generate.assert_not_called()
    assert not (tmp_path / ("full" if full else "screen") / "manifest.json").exists()


@pytest.mark.parametrize("change,message", [
    ({"reader": {"status": "OK", "spec": {"prompt_version": "other"}}}, "reader mismatch"),
    ({"reader_context_text": "different evidence"}, "evidence mismatch"),
])
def test_resume_rejects_mismatched_reader_or_evidence_before_paid_work(monkeypatch, tmp_path, change, message):
    source = trace()
    write_json(tmp_path / "screen/traces" / e.trace_filename("q1"), {**source, **change})
    generate = Mock(side_effect=AssertionError("unexpected generation"))
    monkeypatch.setattr(e, "make_reader", lambda client: object())
    monkeypatch.setattr(e, "make_record", generate)
    plan = {"screen_ids": ["q1"], "reader_spec": source["reader"]["spec"]}
    with pytest.raises(ValueError, match=message):
        e.run_stage(tmp_path, None, {}, {}, {}, plan, {"q1": source}, object())
    generate.assert_not_called()


@pytest.mark.parametrize("selection,audited", [(False, True), (True, False)])
def test_full_generation_requires_selection_and_independent_audit(monkeypatch, tmp_path, selection, audited):
    write_json(tmp_path / "selection.json", {"eligible_for_full": selection})
    write_json(tmp_path / "screen-post-run-audit.json", {"audit_passed": audited})
    provider = Mock(side_effect=AssertionError("must not construct the model"))
    monkeypatch.setattr(e.previous, "model_provider", provider)
    with pytest.raises(e.PilotStopped):
        e.prepare_full(tmp_path, None, {}, {})
    provider.assert_not_called()


def test_full_evidence_hash_is_checked_before_loading_or_regeneration(monkeypatch, tmp_path):
    write_json(tmp_path / "selection.json", {"eligible_for_full": True})
    write_json(tmp_path / "screen-post-run-audit.json", {"audit_passed": True})
    record_path = tmp_path / "offline-full/traces" / e.trace_filename("q1")
    write_json(record_path, trace())
    write_json(tmp_path / "full-evidence-plan.json", {
        "dataset_sha256": "fixture", "trace_sha256": {"q1": e.digest(record_path)}})
    write_json(record_path, trace(context="changed after freezing"))
    load = Mock(side_effect=AssertionError("must validate bytes before loading"))
    monkeypatch.setattr(e, "_load_run", load)
    with pytest.raises(ValueError, match="Frozen full evidence changed"):
        e.prepare_full(tmp_path, SimpleNamespace(sha256="fixture"), {"all_ids": ["q1"]}, {})
    load.assert_not_called()


@pytest.mark.parametrize("paid", [False, True])
def test_offline_or_unaffordable_run_never_reads_credentials_or_contacts_provider(monkeypatch, tmp_path, paid):
    monkeypatch.setattr(sys, "argv", ["experiment", "--output", str(tmp_path)] + (["--paid"] if paid else []))
    monkeypatch.setattr(e, "prepare", lambda out: (object(), {}, {}, {}, {}))
    monkeypatch.setattr(e, "preflight", lambda *args, **kwargs: {"fits_budget": False})
    secrets = Mock(side_effect=AssertionError("must not read credentials"))
    network = Mock(side_effect=AssertionError("must not contact provider"))
    monkeypatch.setattr(e.getpass, "getpass", secrets)
    monkeypatch.setattr(e, "private_get", network)
    if paid:
        with pytest.raises(e.PilotStopped, match="no request sent"):
            e.main()
    else:
        e.main()
    secrets.assert_not_called()
    network.assert_not_called()
