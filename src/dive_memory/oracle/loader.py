"""Strict loader for the official ``longmemeval_oracle.json`` benchmark.

This is *not* the ``LongMemEval_S-cleaned`` loader. The two artifacts share a
row schema and nothing else that matters:

* ``longmemeval_oracle.json`` is the scored benchmark: 500 questions, the
  history pruned to the evidence sessions, and the official ``answer`` used by
  the project group's ``evaluate_qa.py`` judge.
* ``longmemeval_s_cleaned.json`` is a 277 MB long-context corpus used for the
  memory *formation* and *evidence retrieval* experiments. No answer generation
  and no official score.

Mixing them up replaces the 40-point Benchmark with a retrieval metric, which
is the single largest scoring risk in this repository. Everything here is
therefore explicit about which artifact it accepts.

See ``docs/oracle/06-s-cleaned-vs-oracle.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import random
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..eval_adapters.longmemeval import (
    LongMemEvalAdapter,
    LongMemEvalCase,
    LongMemEvalSchemaError,
)
from .contracts import (
    ABSTENTION_SUFFIX,
    ORACLE_ALLOWED_FIELDS,
    ORACLE_DATASET_NAME,
    ORACLE_EXPECTED_CASES,
    ORACLE_QUESTION_TYPES,
    ORACLE_REQUIRED_FIELDS,
)


class OracleLoadError(LongMemEvalSchemaError):
    """Raised when a file is not usable as the official Oracle benchmark."""


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_rows(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OracleLoadError(f"cannot read dataset: {exc}") from exc
    if path.suffix.casefold() == ".jsonl":
        rows: list[Any] = []
        for index, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise OracleLoadError(f"row {index} is not valid JSON: {exc}") from exc
        return rows
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OracleLoadError(f"dataset is not valid JSON: {exc}") from exc
    if not isinstance(loaded, list):
        raise OracleLoadError("dataset root must be a list or JSONL rows")
    return loaded


def validate_oracle_schema(rows: Sequence[Any]) -> None:
    """Reject anything that is not the published Oracle row schema.

    The upper bound matters as much as the lower one: an extra key means the
    file is a different (possibly re-labelled) release, and the run would then
    be reported under the wrong dataset name.
    """

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise OracleLoadError(f"row {index} must be an object")
        keys = set(row)
        missing = ORACLE_REQUIRED_FIELDS - keys
        if missing:
            raise OracleLoadError(
                f"row {index} is missing required Oracle fields: {sorted(missing)!r}"
            )
        extra = keys - ORACLE_ALLOWED_FIELDS
        if extra:
            raise OracleLoadError(
                f"row {index} carries unknown fields {sorted(extra)!r}; this is not the "
                "published longmemeval_oracle.json schema"
            )
        question_id = row["question_id"]
        if not isinstance(question_id, str) or not question_id.strip():
            raise OracleLoadError(f"row {index} has an empty question_id")
        question = row["question"]
        if not isinstance(question, str) or not question.strip():
            raise OracleLoadError(f"row {question_id} has an empty question")
        question_type = row["question_type"]
        if question_type not in ORACLE_QUESTION_TYPES:
            raise OracleLoadError(
                f"row {question_id} has unknown question_type {question_type!r}"
            )


def is_abstention(question_id: str) -> bool:
    return str(question_id).casefold().endswith(ABSTENTION_SUFFIX)


@dataclass(frozen=True, slots=True)
class OracleDataset:
    path: Path
    sha256: str
    cases: tuple[LongMemEvalCase, ...]
    size_bytes: int
    declared_name: str = ORACLE_DATASET_NAME

    @property
    def question_ids(self) -> tuple[str, ...]:
        return tuple(case.question_id for case in self.cases)

    @property
    def question_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.question_type] = counts.get(case.question_type, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def abstention_ids(self) -> tuple[str, ...]:
        return tuple(case.question_id for case in self.cases if is_abstention(case.question_id))

    def require_full_benchmark(self, expected: int = ORACLE_EXPECTED_CASES) -> None:
        if len(self.cases) != expected:
            raise OracleLoadError(
                f"the official benchmark is {expected} questions but the dataset has "
                f"{len(self.cases)}; a partial file must not be reported as a full run"
            )

    def by_id(self) -> dict[str, LongMemEvalCase]:
        return {case.question_id: case for case in self.cases}


def load_oracle_dataset(
    path: str | Path,
    *,
    expected_cases: int | None = ORACLE_EXPECTED_CASES,
) -> OracleDataset:
    """Load and fully validate the official Oracle artifact.

    ``expected_cases=None`` permits a partial file for smoke/pilot runs; the
    runner records the real count in its manifest either way.
    """

    source = Path(path)
    if not source.exists():
        raise OracleLoadError(
            f"Oracle dataset not found at {source}. The official artifact is "
            "longmemeval_oracle.json (LongMemEval, arXiv:2410.10813); place it at "
            "eval/external/longmemeval/longmemeval_oracle.json and record its SHA-256."
        )
    rows = _raw_rows(source)
    validate_oracle_schema(rows)
    cases = LongMemEvalAdapter.load(source)
    if len(cases) != len(rows):
        raise OracleLoadError("row count changed between schema check and parse")
    if expected_cases is not None and len(cases) != expected_cases:
        raise OracleLoadError(
            f"expected {expected_cases} Oracle cases, found {len(cases)}"
        )
    return OracleDataset(
        path=source.resolve(),
        sha256=sha256_file(source),
        cases=tuple(cases),
        size_bytes=source.stat().st_size,
    )


def stratified_selection(
    cases: Sequence[LongMemEvalCase],
    *,
    size: int,
    seed: int,
) -> list[LongMemEvalCase]:
    """Deterministic, question-type-stratified pilot sample.

    Allocation is proportional with largest-remainder rounding, and each
    stratum is ordered by ``question_id`` before sampling so the result does not
    depend on the order of the published file. The seed is recorded in the run
    manifest, which is what makes a pilot reproducible without freezing the
    question list itself.
    """

    if size <= 0:
        raise ValueError("size must be positive")
    if size >= len(cases):
        return list(cases)
    strata: dict[str, list[LongMemEvalCase]] = {}
    for case in sorted(cases, key=lambda item: item.question_id):
        strata.setdefault(case.question_type, []).append(case)
    total = len(cases)
    exact = {name: size * len(group) / total for name, group in strata.items()}
    quota = {name: int(value) for name, value in exact.items()}
    remainder = size - sum(quota.values())
    ordered = sorted(exact, key=lambda name: (-(exact[name] - quota[name]), name))
    for name in ordered[:remainder]:
        quota[name] += 1

    rng = random.Random(seed)
    selected: list[LongMemEvalCase] = []
    for name in sorted(strata):
        take = min(quota.get(name, 0), len(strata[name]))
        selected.extend(rng.sample(strata[name], take))
    # A stratum smaller than its quota would leave the sample short; top up from
    # the remainder so the pilot really has ``size`` questions.
    shortfall = size - len(selected)
    if shortfall > 0:
        chosen = {case.question_id for case in selected}
        pool = [case for case in cases if case.question_id not in chosen]
        selected.extend(rng.sample(pool, min(shortfall, len(pool))))
    return sorted(selected, key=lambda item: item.question_id)


def select_cases(
    cases: Sequence[LongMemEvalCase],
    *,
    question_ids: Iterable[str] | None = None,
    max_cases: int | None = None,
    stratified_size: int | None = None,
    seed: int = 0,
) -> list[LongMemEvalCase]:
    """Apply an explicit id list, a stratified pilot size, or a head slice."""

    selected = list(cases)
    if question_ids is not None:
        wanted = [str(value) for value in question_ids]
        available = {case.question_id for case in cases}
        unknown = [value for value in wanted if value not in available]
        if unknown:
            raise OracleLoadError(f"unknown question ids requested: {unknown!r}")
        index = {case.question_id: case for case in cases}
        selected = [index[value] for value in wanted]
    if stratified_size is not None:
        selected = stratified_selection(selected, size=stratified_size, seed=seed)
    if max_cases is not None:
        selected = selected[: max(0, int(max_cases))]
    return selected
