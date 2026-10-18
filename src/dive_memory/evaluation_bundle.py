"""Offline, immutable handoff of an Oracle submission, never an official score.

The provenance declaration is checked for consistency, not authenticated as
generation evidence. No model, judge, reference answer or credential is copied.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

from .oracle.loader import load_oracle_dataset
from .oracle.validator import validate_submission

FILES = {"submission.jsonl", "provenance.json", "validation.json"}
FIELDS = {
    "schema_version",
    "dataset_sha256",
    "submission_sha256",
    "generation_kind",
    "generation_revision",
    "answer_model",
    "judge_model",
    "dataset_exposure",
    "full_run_usage",
    "limitations",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def validate_provenance(data: dict) -> None:
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError("provenance fields do not match the public declaration schema")
    if data["schema_version"] != "dive-evaluation-provenance-v1":
        raise ValueError("unsupported provenance version")
    for field in ("dataset_sha256", "submission_sha256"):
        if not isinstance(data[field], str) or not re.fullmatch(
            r"[a-f0-9]{64}", data[field]
        ):
            raise ValueError("invalid input hash")
    if data["generation_kind"] not in {"historical_mixed", "new_generation"}:
        raise ValueError("declare historical_mixed or new_generation")
    for field in (
        "generation_revision",
        "answer_model",
        "judge_model",
        "dataset_exposure",
        "limitations",
    ):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError("all disclosure text fields must be nonempty")
    usage = data["full_run_usage"]
    if not isinstance(usage, dict) or set(usage) != {
        "input_tokens",
        "output_tokens",
        "cost_cny",
        "wall_seconds",
    }:
        raise ValueError("full_run_usage requires explicit nulls for unknown metrics")
    for field, value in usage.items():
        if value is None:
            continue
        if (
            type(value) not in (int, float)
            or value < 0
            or not float("-inf") < value < float("inf")
        ):
            raise ValueError("usage must be finite, nonnegative or null")
        if field.endswith("tokens") and type(value) is not int:
            raise ValueError("token counts must be integers")


def build_bundle(
    dataset: Path, submission: Path, provenance: Path, output: Path
) -> dict:
    if output.exists():
        raise ValueError(
            "use a new output directory; previous evidence is never overwritten"
        )
    reference = load_oracle_dataset(dataset)
    report = validate_submission(
        submission, expected_ids=reference.question_ids, require_full=True
    )
    if report.status != "VALID" or report.blank_lines:
        raise ValueError(
            "submission must contain exactly 500 valid, nonblank, unique reference rows"
        )
    declaration = json.loads(provenance.read_text(encoding="utf-8"))
    validate_provenance(declaration)
    if declaration["dataset_sha256"] != digest(dataset) or declaration[
        "submission_sha256"
    ] != digest(submission):
        raise ValueError("provenance hashes differ from the supplied inputs")
    output.mkdir(parents=True)
    # Copy exact submitted bytes. Never rewrite answers or select historical wins.
    (output / "submission.jsonl").write_bytes(submission.read_bytes())
    if digest(output / "submission.jsonl") != declaration["submission_sha256"]:
        raise ValueError(
            "submission changed while packaging; preserve this failed output"
        )
    write_json(output / "provenance.json", declaration)
    validation = report.to_dict()
    validation["path"] = "submission.jsonl"
    write_json(output / "validation.json", validation)
    manifest = {
        "schema_version": "dive-evaluation-bundle-v1",
        "submission_status": "VALID_500_ROWS",
        "official_score": "NOT_EVALUATED",
        "rule_review": "PENDING_PROJECT_GROUP",
        "generation_provenance": "DECLARED_NOT_INDEPENDENTLY_VERIFIED",
        "dataset_sha256": declaration["dataset_sha256"],
        "generation_kind": declaration["generation_kind"],
        "missing_full_run_metrics": [
            key for key, value in declaration["full_run_usage"].items() if value is None
        ],
        "files": {name: digest(output / name) for name in sorted(FILES)},
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def verify_bundle(bundle: Path, dataset: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "dive-evaluation-bundle-v1"
        or set(manifest.get("files", {})) != FILES
    ):
        raise ValueError("unexpected bundle schema or file list")
    if (
        manifest.get("submission_status") != "VALID_500_ROWS"
        or manifest.get("generation_provenance")
        != "DECLARED_NOT_INDEPENDENTLY_VERIFIED"
    ):
        raise ValueError(
            "local bundle status cannot imply independently verified provenance"
        )
    for name, expected in manifest["files"].items():
        if digest(bundle / name) != expected:
            raise ValueError("bundle content hash mismatch")
    declaration = json.loads((bundle / "provenance.json").read_text(encoding="utf-8"))
    validate_provenance(declaration)
    if (
        digest(dataset) != manifest["dataset_sha256"]
        or digest(dataset) != declaration["dataset_sha256"]
    ):
        raise ValueError("reference dataset differs from the frozen dataset")
    if digest(bundle / "submission.jsonl") != declaration["submission_sha256"]:
        raise ValueError("submission differs from its declared origin")
    if (
        manifest.get("official_score") != "NOT_EVALUATED"
        or manifest.get("rule_review") != "PENDING_PROJECT_GROUP"
    ):
        raise ValueError(
            "local bundles cannot certify official scores or rule approval"
        )
    if manifest.get("generation_kind") != declaration["generation_kind"]:
        raise ValueError("generation kind differs from declaration")
    expected_missing = [
        key for key, value in declaration["full_run_usage"].items() if value is None
    ]
    if manifest.get("missing_full_run_metrics") != expected_missing:
        raise ValueError("missing metrics differ from declaration")
    reference = load_oracle_dataset(dataset)
    report = validate_submission(
        bundle / "submission.jsonl",
        expected_ids=reference.question_ids,
        require_full=True,
    )
    if report.status != "VALID" or report.blank_lines:
        raise ValueError("bundle submission is invalid")
    expected_validation = report.to_dict()
    expected_validation["path"] = "submission.jsonl"
    if (
        json.loads((bundle / "validation.json").read_text(encoding="utf-8"))
        != expected_validation
    ):
        raise ValueError("saved validation differs from independent validation")
    return {
        "integrity": "VERIFIED",
        "rows": report.lines,
        "official_score": "NOT_EVALUATED",
        "generation_provenance": "DECLARED_NOT_INDEPENDENTLY_VERIFIED",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--submission", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.verify:
        if any((args.submission, args.provenance, args.output)):
            parser.error("verify cannot be combined with packaging options")
        result = verify_bundle(args.verify, args.dataset)
    else:
        if not all((args.submission, args.provenance, args.output)):
            parser.error("packaging requires submission, provenance and output")
        result = build_bundle(
            args.dataset, args.submission, args.provenance, args.output
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
