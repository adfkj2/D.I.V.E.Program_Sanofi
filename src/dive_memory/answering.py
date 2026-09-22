from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Sequence

from .models import RetrievalItem


class AnswerStatus(StrEnum):
    ANSWER = "ANSWER"
    ABSTAIN = "ABSTAIN"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    memory_id: str
    quote: str
    source_refs: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Any) -> "EvidenceCitation":
        if not isinstance(payload, dict):
            raise ValueError("citation must be an object")
        if set(payload) != {"memory_id", "quote", "source_refs"}:
            raise ValueError("citation fields must be memory_id, quote and source_refs")
        memory_id = str(payload["memory_id"]).strip()
        quote = str(payload["quote"]).strip()
        source_refs = payload["source_refs"]
        if not memory_id or not quote or not isinstance(source_refs, list):
            raise ValueError("citation requires a memory id, quote and source ref list")
        return cls(memory_id, quote, tuple(str(value) for value in source_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "quote": self.quote,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class GroundedAnswerDecision:
    status: AnswerStatus
    answer: str | None
    reason_code: str
    citations: tuple[EvidenceCitation, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    policy_version: str = "grounded-answer-v1"

    @classmethod
    def from_payload(cls, payload: Any, *, policy_version: str = "grounded-answer-v1") -> "GroundedAnswerDecision":
        if not isinstance(payload, dict):
            raise ValueError("reader output must be an object")
        # ``to_dict`` is the documented wire form and carries ``policy_version``
        # alongside the five decision fields, so a persisted decision must be
        # able to be read back. Reader model output legitimately omits it (the
        # reader cannot be trusted to name its own policy), so it stays optional
        # here and falls back to the caller's value.
        required = {"status", "answer", "reason_code", "citations", "missing_requirements"}
        allowed = required | {"policy_version"}
        if not required <= set(payload) or not set(payload) <= allowed:
            raise ValueError("reader output fields do not match grounded-answer-v1")
        if payload.get("policy_version") is not None:
            policy_version = str(payload["policy_version"])
        try:
            status = AnswerStatus(str(payload["status"]))
        except ValueError as exc:
            raise ValueError("reader status must be ANSWER, ABSTAIN or ERROR") from exc
        answer = payload["answer"]
        if answer is not None:
            answer = str(answer).strip() or None
        reason_code = str(payload["reason_code"]).strip()
        citations = payload["citations"]
        missing = payload["missing_requirements"]
        if not reason_code or not isinstance(citations, list) or not isinstance(missing, list):
            raise ValueError("reader reason/citations/missing requirements are malformed")
        return cls(
            status,
            answer,
            reason_code,
            tuple(EvidenceCitation.from_payload(value) for value in citations),
            tuple(str(value) for value in missing),
            policy_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "answer": self.answer,
            "reason_code": self.reason_code,
            "citations": [citation.to_dict() for citation in self.citations],
            "missing_requirements": list(self.missing_requirements),
            "policy_version": self.policy_version,
        }


class GroundedReader(Protocol):
    policy_version: str

    def answer(
        self,
        question: str,
        items: Sequence[RetrievalItem],
        *,
        as_of: str | None = None,
    ) -> GroundedAnswerDecision: ...


def _unsupported(reason: str, *, policy_version: str) -> GroundedAnswerDecision:
    return GroundedAnswerDecision(
        AnswerStatus.ABSTAIN,
        None,
        "UNSUPPORTED_READER_OUTPUT",
        missing_requirements=(reason,),
        policy_version=policy_version,
    )


def validate_grounded_decision(
    decision: GroundedAnswerDecision,
    items: Sequence[RetrievalItem],
) -> GroundedAnswerDecision:
    """Fail closed when a claimed answer is not grounded in returned evidence."""
    if decision.status is AnswerStatus.ERROR:
        return decision
    if decision.status is AnswerStatus.ABSTAIN:
        if decision.answer is not None or decision.citations:
            return _unsupported("an abstention cannot contain an answer or citations",
                                policy_version=decision.policy_version)
        return decision
    if not decision.answer:
        return _unsupported("ANSWER requires non-empty answer text", policy_version=decision.policy_version)
    if not decision.citations:
        return _unsupported("ANSWER requires at least one citation", policy_version=decision.policy_version)

    by_id = {item.memory.id: item for item in items}
    for citation in decision.citations:
        item = by_id.get(citation.memory_id)
        if item is None:
            return _unsupported("citation references a memory outside the retrieval set",
                                policy_version=decision.policy_version)
        if citation.quote not in item.memory.content:
            return _unsupported("citation quote is not a verbatim memory substring",
                                policy_version=decision.policy_version)
        if not citation.source_refs:
            return _unsupported("citation is missing provenance source refs",
                                policy_version=decision.policy_version)
        if not set(citation.source_refs) <= set(item.source_refs):
            return _unsupported("citation source refs are outside the retrieved provenance",
                                policy_version=decision.policy_version)
    return decision


def decide_grounded_answer(
    reader: GroundedReader,
    question: str,
    items: Sequence[RetrievalItem],
    *,
    as_of: str | None = None,
) -> GroundedAnswerDecision:
    policy_version = str(getattr(reader, "policy_version", "grounded-answer-v1"))
    if not items:
        return GroundedAnswerDecision(
            AnswerStatus.ABSTAIN,
            None,
            "NO_RETRIEVAL_CANDIDATES",
            missing_requirements=("No memory candidate was retrieved.",),
            policy_version=policy_version,
        )
    try:
        decision = reader.answer(question, items, as_of=as_of)
    except TimeoutError:
        return GroundedAnswerDecision(AnswerStatus.ERROR, None, "READER_TIMEOUT",
                                      policy_version=policy_version)
    except Exception as exc:
        return GroundedAnswerDecision(
            AnswerStatus.ERROR,
            None,
            "READER_FAILURE",
            missing_requirements=(type(exc).__name__,),
            policy_version=policy_version,
        )
    return validate_grounded_decision(decision, items)


@dataclass(slots=True)
class OpenAICompatibleGroundedReader:
    endpoint: str
    model: str
    api_key: str
    timeout_seconds: float = 30.0
    policy_version: str = "grounded-answer-openai-compatible-v1"

    def answer(
        self,
        question: str,
        items: Sequence[RetrievalItem],
        *,
        as_of: str | None = None,
    ) -> GroundedAnswerDecision:
        evidence = [
            {
                "memory_id": item.memory.id,
                "content": item.memory.content,
                "valid_from": item.memory.valid_from,
                "valid_to": item.memory.valid_to,
                "source_refs": list(item.source_refs),
            }
            for item in items
        ]
        prompt = {
            "question": question,
            "as_of": as_of,
            "evidence": evidence,
            "rules": [
                "Answer only when every required premise is explicitly supported by the evidence.",
                "Similar entities, relations or time periods are not support.",
                "Do not infer zero or absence from a missing fact.",
                "Every answer needs a verbatim quote and source refs from a cited returned memory.",
                "Otherwise return ABSTAIN and list the missing requirements.",
            ],
            "output_schema": {
                "status": "ANSWER|ABSTAIN|ERROR",
                "answer": "string|null",
                "reason_code": "string",
                "citations": [{"memory_id": "string", "quote": "string", "source_refs": ["string"]}],
                "missing_requirements": ["string"],
            },
        }
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You are a conservative grounded memory reader."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw = payload["choices"][0]["message"]["content"]
        parsed = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
        return GroundedAnswerDecision.from_payload(parsed, policy_version=self.policy_version)
