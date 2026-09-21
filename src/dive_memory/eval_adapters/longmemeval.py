from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..evaluation import EvalCase


class LongMemEvalSchemaError(ValueError):
    """Raised when an input row cannot be evaluated without ambiguity."""


def _instant(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    # The official cleaned release uses, for example,
    # ``2023/04/10 (Mon) 23:07`` rather than ISO-8601.
    try:
        return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M")
    except ValueError:
        return None


def _after_cutoff(session_value: str, question_value: str) -> bool:
    session_time = _instant(session_value)
    cutoff = _instant(question_value)
    if session_time is None or cutoff is None:
        return False
    if "/" in session_value and "/" in question_value:
        # The official generator randomizes clock times independently for
        # several temporal tasks.  In the cleaned release 65 sessions are
        # later by clock time but none are on a later calendar day.  Treat the
        # published timestamp as day-precision for the cutoff boundary.
        return session_time.date() > cutoff.date()
    return session_time > cutoff


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LongMemEvalSchemaError(f"{field} must be a non-empty string")
    return value


def _required_answer(row: Mapping[str, Any]) -> str:
    """Return the official answer as text without accepting structured values.

    LongMemEval-cleaned contains numeric temporal answers.  The official judge
    formats these values into a text prompt, so normalizing finite scalar
    numbers to their JSON spelling preserves that protocol.
    """

    value = row.get("answer")
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return rendered
    raise LongMemEvalSchemaError("answer must be a non-empty string or finite number")


@dataclass(frozen=True, slots=True)
class LongMemEvalTurn:
    session_id: str
    session_occurrence: int
    turn_index: int
    role: str
    content: str
    occurred_at: str
    has_answer: bool = False

    @property
    def ref(self) -> str:
        suffix = "" if self.session_occurrence == 1 else f"#{self.session_occurrence}"
        return f"{self.session_id}{suffix}:{self.turn_index}"

    @property
    def occurred_at_iso(self) -> str:
        value = _instant(self.occurred_at)
        if value is None:
            raise LongMemEvalSchemaError(f"unsupported timestamp: {self.occurred_at!r}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()


@dataclass(frozen=True, slots=True)
class LongMemEvalCase:
    question_id: str
    question: str
    answer: str
    question_type: str
    question_date: str
    turns: tuple[LongMemEvalTurn, ...]
    evidence_session_ids: tuple[str, ...]

    @property
    def namespace(self) -> str:
        return f"longmemeval:{self.question_id}"

    @property
    def question_date_iso(self) -> str:
        value = _instant(self.question_date)
        if value is None:
            raise LongMemEvalSchemaError(f"unsupported timestamp: {self.question_date!r}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(turn.ref for turn in self.turns if turn.has_answer)

    @property
    def should_abstain(self) -> bool:
        answer = self.answer.strip().casefold()
        return self.question_id.casefold().endswith("_abs") or self.question_type.casefold() in {
            "abstention", "unanswerable",
        } or answer in {
            "abstain", "unknown", "unanswerable", "i don't know", "cannot be determined",
        }

    def system_input(self) -> Iterable[dict[str, str]]:
        """Yield the writer-visible allow-list, deliberately excluding all gold labels."""

        for turn in self.turns:
            yield {
                "namespace": self.namespace,
                "session_id": turn.session_id,
                "turn_id": turn.ref,
                "role": turn.role,
                "content": turn.content,
                "occurred_at": turn.occurred_at,
            }

    def to_eval_case(self, memory_ids_by_evidence: Mapping[str, list[str]]) -> EvalCase:
        expected: list[str] = []
        if not self.should_abstain:
            lookup_keys = (*self.evidence_refs, *self.evidence_session_ids)
            for key in lookup_keys:
                for memory_id in memory_ids_by_evidence.get(key, []):
                    if memory_id not in expected:
                        expected.append(memory_id)
        return EvalCase(
            case_id=self.question_id,
            namespace=self.namespace,
            question=self.question,
            answer=None if self.should_abstain else self.answer,
            expected_memory_ids=expected,
            should_abstain=self.should_abstain,
            query_type=self.question_type,
        )


class LongMemEvalAdapter:
    """Strict, read-only adapter for LongMemEval-cleaned JSON/JSONL artifacts.

    The adapter never downloads data. A benchmark run must obtain the official
    artifact separately and record its license/version/hash in its manifest.
    """

    @classmethod
    def load(cls, path: str | Path) -> list[LongMemEvalCase]:
        source = Path(path)
        try:
            if source.suffix.casefold() == ".jsonl":
                rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
            else:
                rows = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LongMemEvalSchemaError(f"cannot read LongMemEval artifact: {exc}") from exc
        if not isinstance(rows, list):
            raise LongMemEvalSchemaError("dataset root must be a list or JSONL rows")
        cases = [cls._parse_row(row, index) for index, row in enumerate(rows)]
        ids = [case.question_id for case in cases]
        if len(ids) != len(set(ids)):
            raise LongMemEvalSchemaError("question_id values must be unique")
        return cases

    @staticmethod
    def _parse_row(raw: Any, index: int) -> LongMemEvalCase:
        if not isinstance(raw, Mapping):
            raise LongMemEvalSchemaError(f"row {index} must be an object")
        question_id = _required_text(raw, "question_id")
        question = _required_text(raw, "question")
        answer = _required_answer(raw)
        question_type = _required_text(raw, "question_type")
        question_date = _required_text(raw, "question_date")
        sessions = raw.get("haystack_sessions")
        session_ids = raw.get("haystack_session_ids")
        dates = raw.get("haystack_dates")
        if not all(isinstance(value, list) for value in (sessions, session_ids, dates)):
            raise LongMemEvalSchemaError(f"row {question_id}: haystack fields must be lists")
        if not (len(sessions) == len(session_ids) == len(dates)):
            raise LongMemEvalSchemaError(f"row {question_id}: haystack sessions, ids, and dates must be aligned")
        turns: list[LongMemEvalTurn] = []
        session_occurrences: dict[str, int] = {}
        for session_index, (session, session_id, occurred_at) in enumerate(zip(sessions, session_ids, dates)):
            if not isinstance(session_id, str) or not session_id:
                raise LongMemEvalSchemaError(f"row {question_id}: invalid session id")
            if not isinstance(occurred_at, str) or not occurred_at:
                raise LongMemEvalSchemaError(f"row {question_id}: invalid session date")
            if _after_cutoff(occurred_at, question_date):
                raise LongMemEvalSchemaError(f"row {question_id}: future session {session_id} leaks past cutoff")
            if not isinstance(session, list):
                raise LongMemEvalSchemaError(f"row {question_id}: session {session_index} must be a list")
            session_occurrences[session_id] = session_occurrences.get(session_id, 0) + 1
            for turn_index, turn in enumerate(session):
                if not isinstance(turn, Mapping):
                    raise LongMemEvalSchemaError(f"row {question_id}: turn must be an object")
                role = _required_text(turn, "role")
                content = turn.get("content")
                if not isinstance(content, str):
                    raise LongMemEvalSchemaError("content must be a string")
                turns.append(LongMemEvalTurn(
                    session_id=session_id,
                    session_occurrence=session_occurrences[session_id],
                    turn_index=turn_index,
                    role=role,
                    content=content,
                    occurred_at=occurred_at,
                    has_answer=turn.get("has_answer") is True,
                ))

        evidence = raw.get("answer_session_ids", [])
        if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
            raise LongMemEvalSchemaError(f"row {question_id}: answer_session_ids must be strings")
        unknown_evidence = set(evidence) - set(session_ids)
        if unknown_evidence:
            raise LongMemEvalSchemaError(f"row {question_id}: unknown evidence sessions {sorted(unknown_evidence)!r}")
        return LongMemEvalCase(
            question_id=question_id,
            question=question,
            answer=answer,
            question_type=question_type,
            question_date=question_date,
            turns=tuple(turns),
            evidence_session_ids=tuple(evidence),
        )
