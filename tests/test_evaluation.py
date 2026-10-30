"""Behavioral tests for the independent offline PFE framework."""

import copy
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from wot.evaluation import (
    ARMS,
    Prediction,
    SyntheticMockProvider,
    build_request,
    cluster_bootstrap,
    main,
    run_benchmark,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dataset():
    return json.loads((ROOT / "eval/public_synthetic/sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def protocol():
    return json.loads((ROOT / "eval/protocols/eval_protocol_v0.1.json").read_text(encoding="utf-8"))


def test_four_real_context_arms_and_reproducible_pipeline(dataset, protocol):
    report = run_benchmark(dataset, protocol)
    assert report == run_benchmark(dataset, protocol)
    assert set(report["arms"]) == set(ARMS)
    assert report["paired"]["complete_four_arm_cases"] == 8
    assert report["cost"] == {"provider_calls": 32, "network_calls": 0, "actual_cny": 0.0}
    assert report["persona_fidelity_validated"] is False
    assert report["domain_tested"] is False
    case = next(row for row in dataset["records"] if row["kind"] == "case" and row["split"] == "holdout")
    contexts = {arm: build_request(arm, case, dataset, protocol).context_json for arm in ARMS}
    assert len(set(contexts.values())) == 4


def test_reference_answers_and_other_users_never_reach_provider(dataset, protocol):
    marker = "PRIVATE_REFERENCE_CANARY_862317"
    for record in dataset["records"]:
        if record["kind"] == "case":
            record["reference"]["reasons"].append(marker)

    class InspectingProvider(SyntheticMockProvider):
        def generate(self, request):
            payload = json.dumps(asdict(request))
            assert marker not in payload
            assert "reference" not in asdict(request)
            for entry in json.loads(request.context_json)["entries"]:
                source = next(row for row in dataset["records"] if row["id"] == entry["id"])
                assert source["owner_id"] == request.owner_id
                assert source["split"] == "train"
            return super().generate(request)

    report = run_benchmark(dataset, protocol, InspectingProvider())
    assert all(item["status"] == "COMPLETED" for item in report["ledger"])


@pytest.mark.parametrize("origin", ["SIMULATED_DIALOGUE", "EXTERNAL_SKILL_DATA", "EVALUATION_PRIVATE", "MODEL_INFERENCE"])
def test_contaminated_training_sources_rejected(dataset, origin):
    dataset["records"][0]["source_origin"] = origin
    with pytest.raises(ValueError, match="real-source"):
        validate_dataset(dataset)


def test_session_and_temporal_split_leakage_rejected(dataset):
    memory = next(row for row in dataset["records"] if row["kind"] == "memory")
    case = next(row for row in dataset["records"] if row["kind"] == "case" and row["owner_id"] == memory["owner_id"])
    case["session_id"] = memory["session_id"]
    with pytest.raises(ValueError, match="session crosses"):
        validate_dataset(dataset)
    case["session_id"] = "separate-session"
    case["occurred_at"] = "2025-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="time windows"):
        validate_dataset(dataset)


def test_near_duplicate_dev_holdout_questions_rejected(dataset):
    dev = next(row for row in dataset["records"] if row["kind"] == "case" and row["split"] == "dev")
    holdout = next(row for row in dataset["records"] if row["kind"] == "case" and row["split"] == "holdout" and row["owner_id"] == dev["owner_id"])
    holdout["question"] = dev["question"] + "?"
    with pytest.raises(ValueError, match="Near-duplicate"):
        validate_dataset(dataset)


@pytest.mark.parametrize("field,value", [("subject_id", "another-user"), ("consent_purpose", "SIMULATION")])
def test_owner_and_purpose_grants_required(dataset, field, value):
    dataset["records"][0][field] = value
    with pytest.raises(ValueError):
        validate_dataset(dataset)


def test_one_provider_receives_identical_frozen_generation_settings(dataset, protocol):
    received = []

    class RecordingProvider(SyntheticMockProvider):
        def generate(self, request):
            received.append(request)
            return super().generate(request)

    run_benchmark(dataset, protocol, RecordingProvider())
    assert {request.arm for request in received} == set(ARMS)
    assert len({(r.provider, r.model, r.temperature, r.max_output_tokens, r.max_context_chars, r.prompt_version) for r in received}) == 1
    assert all(len(r.context_json) <= r.max_context_chars for r in received)
    request = received[0]
    assert RecordingProvider().generate(request) == RecordingProvider().generate(replace(request, arm="D" if request.arm != "D" else "B0"))


def test_missing_failures_and_fabricated_references_remain_in_denominator(dataset, protocol):
    class FaultyProvider(SyntheticMockProvider):
        def generate(self, request):
            if request.arm == "B0":
                return None
            if request.arm == "B1":
                raise TimeoutError("private text must not appear in ledger")
            if request.arm == "D":
                return Prediction("ABSTAIN", (), ("holdout-secret-reference",))
            return super().generate(request)

    report = run_benchmark(dataset, protocol, FaultyProvider())
    assert report["arms"]["B0"]["missing"] == 8
    assert report["arms"]["B1"]["failed"] == 8
    assert report["arms"]["D"]["failed"] == 8
    assert report["paired"]["expected_cases"] == 8
    assert report["paired"]["complete_four_arm_cases"] == 0
    assert report["paired"]["bootstrap"]["ci95"] is None
    assert "private text" not in json.dumps(report)


def test_user_session_bootstrap_does_not_treat_repeated_items_as_people():
    rows = [
        {"owner_id": "a", "session_id": "a1", "difference": 1.0},
        {"owner_id": "b", "session_id": "b1", "difference": -1.0},
    ]
    result = cluster_bootstrap(rows, seed=17, iterations=200)
    repeated = [*rows, *[copy.copy(rows[0]) for _ in range(100)]]
    duplicated_result = cluster_bootstrap(repeated, seed=17, iterations=200)
    assert result["estimate"] == duplicated_result["estimate"] == 0.0
    assert result["ci95"] == duplicated_result["ci95"]
    assert duplicated_result["users"] == 2
    assert duplicated_result["sessions"] == 2


def test_real_dataset_external_provider_and_private_cli_paths_rejected(dataset, protocol):
    dataset["classification"] = "REAL_PRIVATE"
    with pytest.raises(ValueError, match="public synthetic"):
        run_benchmark(dataset, protocol)
    dataset["classification"] = "PUBLIC_SYNTHETIC"
    protocol["generation"]["provider"] = "external-provider"
    with pytest.raises(ValueError, match="external"):
        run_benchmark(dataset, protocol)
    with pytest.raises(SystemExit):
        main(["--output", "unused.json", "--dataset", "eval/private/answers.json"])


def test_cli_writes_report_and_marks_unvalidated(tmp_path):
    output = tmp_path / "report.json"
    assert main(["--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["classification"] == "SYNTHETIC_FRAMEWORK_VALIDATION"
    assert not report["persona_fidelity_validated"]
