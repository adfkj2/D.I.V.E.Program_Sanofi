from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
from typing import Any, Mapping

from .models import ALLOWED_MEMORY_KINDS, EvidenceState
from .normalization import normalize_predicate, normalize_text
from .temporal import parse_instant, require_instant


class CandidateValidationError(ValueError):
    pass


class Explicitness(StrEnum):
    EXPLICIT = "EXPLICIT"
    IMPLICIT = "IMPLICIT"
    INFERRED = "INFERRED"


ALLOWED_DURABILITIES = frozenset({"ephemeral", "short_term", "medium_term", "long_term", "permanent"})
_FIELDS = frozenset({
    "schema_version", "memory_type", "subject", "predicate", "value", "normalized_value",
    "valid_from", "valid_to", "confidence", "importance", "durability", "evidence_state",
    "explicitness", "evidence_spans", "entities", "relations",
})


def _bounded(raw: Any, field: str) -> float:
    if isinstance(raw, bool):
        raise CandidateValidationError(f"{field} must be a number from 0 to 1")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(f"{field} must be a number from 0 to 1") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise CandidateValidationError(f"{field} must be a finite number from 0 to 1")
    return value


def _string(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not normalize_text(raw):
        raise CandidateValidationError(f"{field} must be a non-empty string")
    return normalize_text(raw)


def _strings(raw: Any, field: str, *, non_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(value, str) or not normalize_text(value) for value in raw):
        raise CandidateValidationError(f"{field} must be a list of non-empty strings")
    values = tuple(normalize_text(value) for value in raw)
    if non_empty and not values:
        raise CandidateValidationError(f"{field} must not be empty")
    return values


@dataclass(frozen=True, slots=True)
class CandidateMemoryV1:
    schema_version: str
    memory_type: str
    subject: str
    predicate: str
    value: str
    normalized_value: str
    valid_from: str | None
    valid_to: str | None
    confidence: float
    importance: float
    durability: str
    evidence_state: EvidenceState
    explicitness: Explicitness
    evidence_spans: tuple[str, ...]
    entities: tuple[str, ...]
    relations: tuple[str, ...]
    source_event_ids: tuple[str, ...] = ()

    @classmethod
    def from_provider_payload(cls, payload: Mapping[str, Any], *, source_text: str) -> "CandidateMemoryV1":
        if not isinstance(payload, Mapping):
            raise CandidateValidationError("candidate must be an object")
        unknown = set(payload) - _FIELDS
        missing = _FIELDS - set(payload)
        if unknown or missing:
            raise CandidateValidationError(
                f"candidate fields mismatch; missing={sorted(missing)!r}, unknown={sorted(unknown)!r}"
            )
        if payload["schema_version"] != "candidate-memory-v1":
            raise CandidateValidationError("unsupported candidate schema_version")
        memory_type = _string(payload["memory_type"], "memory_type")
        if memory_type not in ALLOWED_MEMORY_KINDS:
            raise CandidateValidationError(f"unknown memory_type: {memory_type}")
        durability = _string(payload["durability"], "durability")
        if durability not in ALLOWED_DURABILITIES:
            raise CandidateValidationError(f"unknown durability: {durability}")
        try:
            evidence_state = EvidenceState(str(payload["evidence_state"]))
        except ValueError as exc:
            raise CandidateValidationError("unknown evidence_state") from exc
        try:
            explicitness = Explicitness(str(payload["explicitness"]))
        except ValueError as exc:
            raise CandidateValidationError("unknown explicitness") from exc
        valid_from = require_instant(payload["valid_from"], "valid_from")
        valid_to = require_instant(payload["valid_to"], "valid_to")
        if valid_from and valid_to and parse_instant(valid_from) >= parse_instant(valid_to):  # type: ignore[operator]
            raise CandidateValidationError("valid_from must be earlier than valid_to")
        evidence_spans = _strings(payload["evidence_spans"], "evidence_spans", non_empty=True)
        normalized_source = normalize_text(source_text, casefold=True)
        if any(normalize_text(span, casefold=True) not in normalized_source for span in evidence_spans):
            raise CandidateValidationError("evidence span is not present in source text")

        raw_normalized_value = _string(payload["normalized_value"], "normalized_value")
        return cls(
            schema_version="candidate-memory-v1",
            memory_type=memory_type,
            subject=_string(payload["subject"], "subject"),
            predicate=normalize_predicate(_string(payload["predicate"], "predicate")),
            value=_string(payload["value"], "value"),
            normalized_value=normalize_text(raw_normalized_value, casefold=True),
            valid_from=valid_from,
            valid_to=valid_to,
            confidence=_bounded(payload["confidence"], "confidence"),
            importance=_bounded(payload["importance"], "importance"),
            durability=durability,
            evidence_state=evidence_state,
            explicitness=explicitness,
            evidence_spans=evidence_spans,
            entities=_strings(payload["entities"], "entities"),
            relations=_strings(payload["relations"], "relations"),
        )

    def with_sources(self, source_event_ids: list[str] | tuple[str, ...]) -> "CandidateMemoryV1":
        sources = tuple(dict.fromkeys(str(value) for value in source_event_ids if str(value).strip()))
        return replace(self, source_event_ids=sources)

    def validate_for_commit(self) -> None:
        if not self.source_event_ids:
            raise CandidateValidationError("at least one source event is required before commit")

    def structured_content(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "normalized_value": self.normalized_value,
            "explicitness": self.explicitness.value,
            "evidence_spans": list(self.evidence_spans),
            "entities": list(self.entities),
            "relations": list(self.relations),
            "candidate_schema_version": self.schema_version,
        }
