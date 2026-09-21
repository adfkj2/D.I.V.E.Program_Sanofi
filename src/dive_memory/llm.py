from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .candidate import CandidateMemoryV1, CandidateValidationError
from .extraction import Candidate, GatePolicy, extract_candidates
from .gate import GateAction, GateDecision, decide


class ExtractionOutcomeCode(StrEnum):
    EMPTY = "EMPTY"
    POLICY_SKIP = "POLICY_SKIP"
    MALFORMED = "MALFORMED"
    TIMEOUT = "TIMEOUT"
    FALLBACK_USED = "FALLBACK_USED"
    COMMITTED = "COMMITTED"


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    code: ExtractionOutcomeCode
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""
    provider: str = ""
    model: str = ""
    prompt_version: str = "candidate-extraction-v1"
    schema_version: str = "candidate-memory-v1"
    fallback_used: bool = False


class ExtractionProvider(Protocol):
    def extract(
        self, text: str, *, observed_at: str | None = None, explicit: bool = False,
        source_type: str | None = None,
    ) -> list[Candidate]: ...


class HeuristicExtractionProvider:
    # Advertises that this provider understands the ``source_type`` keyword.
    # ``MemoryService`` checks the flag before passing it, so pre-v2 custom
    # extractors keep their original signatures.
    supports_source_type = True

    def __init__(self, gate: GatePolicy | None = None) -> None:
        # ``gate`` is optional so every existing call site keeps the v1
        # keyword policy; pass a ``SemanticGate`` to enable v2.
        self.gate: GatePolicy = gate or decide

    def extract_outcome(
        self, text: str, *, observed_at: str | None = None, explicit: bool = False,
        source_type: str | None = None,
    ) -> ExtractionOutcome:
        candidates = tuple(extract_candidates(text, explicit=explicit, observed_at=observed_at,
                                              gate=self.gate, source_type=source_type))
        if candidates:
            code = ExtractionOutcomeCode.COMMITTED
            reason = "heuristic candidates accepted"
        else:
            if getattr(self.gate, "supports_source_type", False):
                decision = self.gate(text, explicit=explicit, source_type=source_type)
            else:
                decision = self.gate(text, explicit=explicit)
            code = ExtractionOutcomeCode.EMPTY if decision.reason_code == "EMPTY" else ExtractionOutcomeCode.POLICY_SKIP
            reason = decision.reason
        return ExtractionOutcome(code, candidates, reason, type(self).__name__, "heuristic-v1")

    def extract(self, text: str, *, observed_at: str | None = None, explicit: bool = False,
                source_type: str | None = None) -> list[Candidate]:
        return list(self.extract_outcome(text, observed_at=observed_at, explicit=explicit,
                                         source_type=source_type).candidates)


@dataclass(slots=True)
class OpenAICompatibleExtractionProvider:
    endpoint: str
    model: str
    api_key: str
    timeout_seconds: float = 30.0
    prompt_version: str = "candidate-extraction-v1"
    gate: GatePolicy = field(default=decide)
    # See ``HeuristicExtractionProvider``: opt in so the service forwards
    # ``source_type``. The gate itself is still free to ignore it.
    supports_source_type: bool = field(default=True, init=False)

    def _fallback(self, text: str, *, observed_at: str | None, explicit: bool,
                  reason: str, source_type: str | None = None) -> ExtractionOutcome:
        candidates = tuple(extract_candidates(text, explicit=explicit, observed_at=observed_at,
                                              gate=self.gate, source_type=source_type))
        return ExtractionOutcome(
            ExtractionOutcomeCode.FALLBACK_USED,
            candidates,
            reason,
            type(self).__name__,
            self.model,
            self.prompt_version,
            "candidate-memory-v1",
            True,
        )

    def extract_outcome(
        self, text: str, *, observed_at: str | None = None, explicit: bool = False,
        source_type: str | None = None,
    ) -> ExtractionOutcome:
        prompt = (
            "Extract only durable user memories explicitly supported by the source. Return a JSON array. "
            "Every object must have exactly these fields: schema_version='candidate-memory-v1', "
            "memory_type, subject, predicate, value, normalized_value, valid_from, valid_to, confidence, "
            "importance, durability, evidence_state, explicitness, evidence_spans, entities, relations. "
            "Evidence spans must be verbatim substrings. Return [] when no durable memory is supported.\n"
            "Source: " + text
        )
        body = json.dumps({"model": self.model, "temperature": 0, "messages": [
            {"role": "system", "content": "You are a conservative structured memory candidate extractor."},
            {"role": "user", "content": prompt},
        ]}).encode()
        request = urllib.request.Request(self.endpoint, data=body, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, OSError) as exc:
            return self._fallback(text, observed_at=observed_at, explicit=explicit,
                                  reason=f"provider unavailable: {type(exc).__name__}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return ExtractionOutcome(ExtractionOutcomeCode.MALFORMED, reason=f"response envelope: {exc}",
                                     provider=type(self).__name__, model=self.model,
                                     prompt_version=self.prompt_version)

        try:
            raw = payload["choices"][0]["message"]["content"]
            values = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
            if not isinstance(values, list):
                raise CandidateValidationError("response root must be an array")
            strict = [CandidateMemoryV1.from_provider_payload(value, source_text=text) for value in values]
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, CandidateValidationError) as exc:
            return ExtractionOutcome(ExtractionOutcomeCode.MALFORMED, reason=f"candidate validation: {exc}",
                                     provider=type(self).__name__, model=self.model,
                                     prompt_version=self.prompt_version)

        if not strict:
            return ExtractionOutcome(ExtractionOutcomeCode.EMPTY, reason="provider returned no candidates",
                                     provider=type(self).__name__, model=self.model,
                                     prompt_version=self.prompt_version)

        candidates: list[Candidate] = []
        for value in strict:
            # v1 gate policies take no ``source_type``; only policies that
            # opt in via ``supports_source_type`` receive the provenance.
            if getattr(self.gate, "supports_source_type", False):
                gate = self.gate(f"{value.predicate}: {value.value}", explicit=explicit,
                                 source_type=source_type)
            else:
                gate = self.gate(f"{value.predicate}: {value.value}", explicit=explicit)
            if not gate.accepted:
                continue
            calibrated = GateDecision(
                True,
                value.importance,
                value.confidence,
                min(1.0, value.importance + 0.1),
                value.durability,
                gate.reason,
                GateAction.WRITE,
                gate.reason_code,
                {**gate.features, "importance": value.importance, "source_confidence": value.confidence},
                gate.policy_version,
            )
            candidates.append(Candidate(
                content=f"{value.predicate}: {value.value}",
                kind=value.memory_type,
                evidence_state=value.evidence_state.value,
                structured_content=value.structured_content(),
                decision=calibrated,
                valid_from=value.valid_from or observed_at,
                valid_to=value.valid_to,
            ))
        if not candidates:
            return ExtractionOutcome(ExtractionOutcomeCode.POLICY_SKIP, reason="all valid candidates failed policy",
                                     provider=type(self).__name__, model=self.model,
                                     prompt_version=self.prompt_version)
        return ExtractionOutcome(ExtractionOutcomeCode.COMMITTED, tuple(candidates), "strict candidates accepted",
                                 type(self).__name__, self.model, self.prompt_version)

    def extract(self, text: str, *, observed_at: str | None = None, explicit: bool = False,
                source_type: str | None = None) -> list[Candidate]:
        return list(self.extract_outcome(text, observed_at=observed_at, explicit=explicit,
                                         source_type=source_type).candidates)
