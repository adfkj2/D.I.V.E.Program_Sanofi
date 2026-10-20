"""Oracle-protocol contracts: field allow-lists, leakage guards, failure taxonomy.

This module is the single source of truth for the *boundary* between what the
memory pipeline may see and what only the evaluator may see. It is deliberately
free of imports from the rest of the package so that tests can import it in
isolation and so that the guard cannot be weakened by accident from elsewhere.

See ``docs/oracle/01-leakage-design.md`` for the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from ..reading.contracts import (
    CANONICAL_ABSTENTION_HYPOTHESIS as CANONICAL_ABSTENTION_HYPOTHESIS,
)
from ..reading.contracts import (
    HypothesisOrigin as HypothesisOrigin,
)
from ..reading.contracts import (
    LeakageError as LeakageError,
)
from ..reading.contracts import (
    ReaderStatus as ReaderStatus,
)

# --- Provenance of the official artifacts -------------------------------------

ORACLE_DATASET_NAME = "longmemeval_oracle"
ORACLE_DATASET_LICENSE = "MIT (LongMemEval, arXiv:2410.10813)"
ORACLE_EXPECTED_CASES = 500
ORACLE_ABSTENTION_CASES = 30
ABSTENTION_SUFFIX = "_abs"

# The nine fields the official ``longmemeval_oracle.json`` release publishes.
ORACLE_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "question_id",
    "question_type",
    "question",
    "answer",
    "question_date",
    "haystack_dates",
    "haystack_session_ids",
    "haystack_sessions",
    "answer_session_ids",
})
ORACLE_ALLOWED_FIELDS = ORACLE_REQUIRED_FIELDS

ORACLE_QUESTION_TYPES: frozenset[str] = frozenset({
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
})

# --- The leakage boundary -----------------------------------------------------

#: Exactly the keys ``LongMemEvalCase.system_input()`` may yield. The writer
#: path asserts this set on every row it is handed, so a future change to the
#: adapter that started forwarding gold cannot pass CI.
WRITER_VISIBLE_FIELDS: frozenset[str] = frozenset({
    "namespace",
    "session_id",
    "turn_id",
    "role",
    "content",
    "occurred_at",
})

#: Fields that exist only to score a run. None of these may reach the extractor,
#: the write gate, the memory store, the retriever, the context packer, the
#: reader prompt, or any trace file.
EVALUATOR_ONLY_FIELDS: frozenset[str] = frozenset({
    "answer",
    "answer_session_ids",
    "has_answer",
    "question_type",
    "evidence_refs",
    "evidence_session_ids",
    "should_abstain",
    "gold_evidence_refs",
    "gold_turn_refs",
})

#: The two keys an official submission line may contain, and nothing else.
SUBMISSION_FIELDS: frozenset[str] = frozenset({"question_id", "hypothesis"})

#: Hypothesis emitted when the reader refuses, errors, or times out. Uniform
#: for every question -- it is a template, not a per-question answer, so it
#: cannot be mistaken for hard-coding. See appendix F of the student handbook.




def _yield_leaked_keys(payload: Any, forbidden: frozenset[str], path: str) -> Iterable[str]:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key) in forbidden:
                yield here
            yield from _yield_leaked_keys(value, forbidden, here)
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield from _yield_leaked_keys(value, forbidden, f"{path}[{index}]")


def find_evaluator_only_fields(payload: Any) -> list[str]:
    """Return the JSON paths under ``payload`` that carry evaluator-only keys."""

    return sorted(_yield_leaked_keys(payload, EVALUATOR_ONLY_FIELDS, ""))


def assert_writer_visible(rows: Sequence[Mapping[str, Any]]) -> None:
    """Fail closed unless every row is exactly the writer allow-list."""

    for index, row in enumerate(rows):
        keys = set(row)
        leaked = find_evaluator_only_fields(row)
        if leaked:
            raise LeakageError(
                f"writer input row {index} carries evaluator-only fields: {leaked!r}"
            )
        if keys != WRITER_VISIBLE_FIELDS:
            raise LeakageError(
                "writer input row "
                f"{index} has keys {sorted(keys)!r}; the allow-list is "
                f"{sorted(WRITER_VISIBLE_FIELDS)!r}"
            )


def assert_no_gold_in_text(text: str, sentinels: Iterable[str]) -> None:
    """Fail closed when a gold sentinel appears verbatim in writer text.

    Used by the leakage tests with deliberate sentinel answers: if any stage
    concatenates a gold label into a prompt, the sentinel shows up here.
    """

    haystack = text or ""
    for sentinel in sentinels:
        if sentinel and sentinel in haystack:
            raise LeakageError(f"gold sentinel {sentinel!r} reached a writer surface")


# --- Failure taxonomy ---------------------------------------------------------


class Stage(StrEnum):
    """Where in ingest -> retrieve -> read a question failed."""

    LOAD = "LOAD"
    FORMATION = "FORMATION"
    RETRIEVAL = "RETRIEVAL"
    PACKING = "PACKING"
    READING = "READING"
    WRITING = "WRITING"
    UNKNOWN = "UNKNOWN"




#: How a hypothesis that ends up in the submission was produced. This is what
#: stops an error fallback from being silently counted as a real answer.


@dataclass(frozen=True, slots=True)
class OracleFailure:
    question_id: str
    stage: Stage
    error_type: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "stage": self.stage.value,
            "error_type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
        }


class OracleResumeConflictError(RuntimeError):
    """Raised when a resume request does not match the artefact on disk."""


class OracleSubmissionError(RuntimeError):
    """Raised when a submission JSONL fails validation."""
