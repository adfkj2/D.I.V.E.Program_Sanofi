"""Submission failures must never be certified as a complete benchmark."""

import json

import pytest

from dive_memory.evaluation_bundle import build_bundle, digest, verify_bundle
from dive_memory.oracle.validator import validate_submission


@pytest.mark.parametrize("count", [0, 1, 499, 501])
def test_full_submission_rejects_wrong_count_without_reference(tmp_path, count):
    path = tmp_path / "submission.jsonl"
    path.write_text(
        "".join(
            json.dumps({"question_id": str(i), "hypothesis": "x"}) + "\n"
            for i in range(count)
        ),
        encoding="utf-8",
    )
    assert validate_submission(path, require_full=True).status == "INVALID"


def test_full_submission_cannot_be_satisfied_by_partial_reference(tmp_path):
    path = tmp_path / "submission.jsonl"
    path.write_text('{"question_id":"one","hypothesis":"x"}\n', encoding="utf-8")
    assert (
        validate_submission(path, expected_ids=["one"], require_full=True).status
        == "INVALID"
    )


def test_duplicate_json_keys_are_not_silently_overwritten(tmp_path):
    path = tmp_path / "submission.jsonl"
    path.write_text(
        '{"question_id":"one","hypothesis":"old","hypothesis":"new"}\n',
        encoding="utf-8",
    )
    assert validate_submission(path).status == "INVALID"


@pytest.fixture
def bundle_inputs(tmp_path):
    dataset = tmp_path / "oracle.json"
    rows = [
        {
            "question_id": str(i),
            "question_type": "single-session-user",
            "question": "Which color?",
            "answer": "blue",
            "question_date": "2026-01-02",
            "haystack_dates": ["2026-01-01"],
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[{"role": "user", "content": "The color is blue."}]],
            "answer_session_ids": ["s1"],
        }
        for i in range(500)
    ]
    dataset.write_text(json.dumps(rows), encoding="utf-8")
    submission = tmp_path / "answers.jsonl"
    submission.write_text(
        "".join(
            json.dumps({"question_id": r["question_id"], "hypothesis": "blue"}) + "\n"
            for r in rows
        ),
        encoding="utf-8",
    )
    provenance = tmp_path / "provenance.json"
    declaration = {
        "schema_version": "dive-evaluation-provenance-v1",
        "dataset_sha256": digest(dataset),
        "submission_sha256": digest(submission),
        "generation_kind": "historical_mixed",
        "generation_revision": "fixture-only",
        "answer_model": "fixture",
        "judge_model": "not run",
        "dataset_exposure": "developer fixture",
        "full_run_usage": {
            "input_tokens": None,
            "output_tokens": None,
            "cost_cny": None,
            "wall_seconds": None,
        },
        "limitations": "Synthetic integration fixture, no model and no score.",
    }
    provenance.write_text(json.dumps(declaration), encoding="utf-8")
    return dataset, submission, provenance, tmp_path / "bundle"


def test_bundle_preserves_bytes_unknown_usage_and_unofficial_status(bundle_inputs):
    dataset, submission, provenance, output = bundle_inputs
    report = build_bundle(*bundle_inputs)
    assert (output / "submission.jsonl").read_bytes() == submission.read_bytes()
    assert len(report["missing_full_run_metrics"]) == 4
    assert verify_bundle(output, dataset)["rows"] == 500
    assert report["official_score"] == "NOT_EVALUATED"
    with pytest.raises(ValueError, match="new output"):
        build_bundle(*bundle_inputs)


@pytest.mark.parametrize(
    "mutation", ["submission", "dataset", "official", "missing", "path"]
)
def test_bundle_rejects_tampered_content_and_claims(bundle_inputs, mutation):
    dataset, _, _, output = bundle_inputs
    build_bundle(*bundle_inputs)
    if mutation == "submission":
        with (output / "submission.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("\n")
    elif mutation == "dataset":
        dataset.write_text("[]", encoding="utf-8")
    else:
        path = output / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "official":
            manifest["official_score"] = 100
        elif mutation == "missing":
            manifest["missing_full_run_metrics"] = []
        else:
            manifest["files"]["../private"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_bundle(output, dataset)


def test_bundle_rejects_mismatched_provenance_and_private_fields(bundle_inputs):
    _, _, provenance, output = bundle_inputs
    data = json.loads(provenance.read_text(encoding="utf-8"))
    data["submission_sha256"] = "0" * 64
    provenance.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="hashes"):
        build_bundle(*bundle_inputs)
    assert not output.exists()
    data["api_key"] = "must-not-be-copied"
    provenance.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        build_bundle(*bundle_inputs)
    assert not output.exists()
