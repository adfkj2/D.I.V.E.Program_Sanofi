"""Independent validator for the official Oracle submission JSONL.

The submission file is what the project group scores, so it is validated by a
module that does not share code with the runner that wrote it. The checks are
the ones the handbook's appendix C submission contract implies:

* every line is a JSON object;
* every object carries **exactly** ``question_id`` and ``hypothesis``;
* both values are non-empty strings;
* question ids are unique and match the reference set exactly (no missing, no
  unexpected);
* nothing else is on the line -- no index, no score, no trace.

``VALID`` means "well formed and complete", not "correct". Overall Accuracy is
a separate question answered by the project group's judge.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .contracts import ORACLE_EXPECTED_CASES, SUBMISSION_FIELDS


@dataclass(frozen=True, slots=True)
class SubmissionReport:
    path: str
    lines: int
    parsed_lines: int
    unique_ids: int
    expected_count: int | None
    coverage: float | None
    duplicate_ids: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()
    unexpected_ids: tuple[str, ...] = ()
    empty_hypotheses: tuple[str, ...] = ()
    non_string_fields: tuple[str, ...] = ()
    field_violations: tuple[str, ...] = ()
    malformed_lines: tuple[str, ...] = ()
    blank_lines: int = 0
    count_mismatch: bool = False

    @property
    def status(self) -> str:
        problems = (
            self.duplicate_ids or self.missing_ids or self.unexpected_ids
            or self.empty_hypotheses or self.non_string_fields
            or self.field_violations or self.malformed_lines or self.count_mismatch
        )
        return "INVALID" if problems else "VALID"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "dive-oracle-submission-validation-v1",
            "path": self.path,
            "status": self.status,
            "lines": self.lines,
            "parsed_lines": self.parsed_lines,
            "unique_ids": self.unique_ids,
            "expected_count": self.expected_count,
            "coverage": self.coverage,
            "duplicate_count": len(self.duplicate_ids),
            "missing_count": len(self.missing_ids),
            "unexpected_count": len(self.unexpected_ids),
            "empty_hypothesis_count": len(self.empty_hypotheses),
            "non_string_field_count": len(self.non_string_fields),
            "field_violation_count": len(self.field_violations),
            "malformed_line_count": len(self.malformed_lines),
            "blank_lines": self.blank_lines,
            "count_mismatch": self.count_mismatch,
            "duplicate_ids": list(self.duplicate_ids[:50]),
            "missing_ids": list(self.missing_ids[:50]),
            "unexpected_ids": list(self.unexpected_ids[:50]),
            "empty_hypotheses": list(self.empty_hypotheses[:50]),
            "non_string_fields": list(self.non_string_fields[:50]),
            "field_violations": list(self.field_violations[:50]),
            "malformed_lines": list(self.malformed_lines[:50]),
        }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def read_submission(path: str | Path) -> list[dict[str, Any]]:
    """Read a submission, preserving every parsed row for the analysis stage."""

    rows: list[dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("a submission line must be a JSON object")
        rows.append(payload)
    return rows


def validate_submission(
    path: str | Path,
    *,
    expected_ids: Iterable[str] | None = None,
    require_full: bool = False,
) -> SubmissionReport:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"submission not found: {source}")
    text = source.read_text(encoding="utf-8")

    duplicates: list[str] = []
    missing: list[str] = []
    unexpected: list[str] = []
    empty: list[str] = []
    non_string: list[str] = []
    field_violations: list[str] = []
    malformed: list[str] = []
    seen: dict[str, int] = {}
    lines = 0
    parsed = 0
    blank = 0

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            blank += 1
            continue
        lines += 1
        try:
            payload = json.loads(line, object_pairs_hook=_unique_object)
        except ValueError:
            malformed.append(f"line {number}: invalid JSON or duplicate object key")
            continue
        if not isinstance(payload, dict):
            malformed.append(f"line {number}: not a JSON object")
            continue
        parsed += 1
        keys = set(payload)
        if keys != SUBMISSION_FIELDS:
            field_violations.append(
                f"line {number}: keys {sorted(keys)!r} != {sorted(SUBMISSION_FIELDS)!r}"
            )
            continue
        question_id = payload["question_id"]
        hypothesis = payload["hypothesis"]
        if not isinstance(question_id, str) or not isinstance(hypothesis, str):
            non_string.append(f"line {number}: question_id/hypothesis must both be strings")
            continue
        if not question_id.strip():
            non_string.append(f"line {number}: question_id is blank")
            continue
        if not hypothesis.strip():
            empty.append(question_id)
        seen[question_id] = seen.get(question_id, 0) + 1

    duplicates = sorted(key for key, count in seen.items() if count > 1)

    expected_count: int | None = None
    coverage: float | None = None
    if expected_ids is not None:
        expected = list(dict.fromkeys(str(value) for value in expected_ids))
        expected_count = len(expected)
        expected_set = set(expected)
        missing = [value for value in expected if value not in seen]
        unexpected = [value for value in sorted(seen) if value not in expected_set]
        coverage = (expected_count - len(missing)) / expected_count if expected_count else 0.0
    elif require_full:
        expected_count = ORACLE_EXPECTED_CASES
        coverage = len(seen) / ORACLE_EXPECTED_CASES

    return SubmissionReport(
        path=str(source),
        lines=lines,
        parsed_lines=parsed,
        unique_ids=len(seen),
        expected_count=expected_count,
        coverage=coverage,
        duplicate_ids=tuple(duplicates),
        missing_ids=tuple(missing),
        unexpected_ids=tuple(unexpected),
        empty_hypotheses=tuple(empty),
        non_string_fields=tuple(non_string),
        field_violations=tuple(field_violations),
        malformed_lines=tuple(malformed),
        blank_lines=blank,
        count_mismatch=require_full and (
            len(seen) != ORACLE_EXPECTED_CASES
            or lines != ORACLE_EXPECTED_CASES
            or expected_count != ORACLE_EXPECTED_CASES
        ),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m dive_memory.oracle.validator",
        description="Validate the official Oracle submission JSONL.",
    )
    parser.add_argument("submission")
    parser.add_argument("--dataset", help="longmemeval_oracle.json, for the reference id set")
    parser.add_argument("--output", help="also write the validation report here")
    parser.add_argument("--require-full", action="store_true",
                        help="require 500 unique ids even without --dataset")
    args = parser.parse_args(argv)

    expected_ids: list[str] | None = None
    if args.dataset:
        from .loader import load_oracle_dataset

        expected_ids = list(load_oracle_dataset(args.dataset, expected_cases=None).question_ids)
    report = validate_submission(args.submission, expected_ids=expected_ids,
                                 require_full=args.require_full)
    payload = report.to_dict()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if report.status != "VALID":
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
