"""Plan, gather, synthesize and review over a bounded admitted-evidence workspace.

Each model phase has one output schema. Search never replaces the working set,
and grounded draft citations are retained for review. No benchmark labels,
archives, provider configuration or hidden model retries are available here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from time import perf_counter
from typing import Any, Sequence

from ..answering import GroundedAnswerDecision
from ..document_chunks import select_document_chunks
from ..models import RetrievalItem
from .contracts import EvidenceSource, ReaderStatus
from .deliberation import (
    LITERAL_PROTOCOL,
    DeliberationProtocol,
    EvidencePlan,
)
from .evidence import build_evidence_views, render_evidence_block
from .execution import DeliberationExecution, PhaseCall, completion_problem
from .reader import (
    ChatClient,
    ReaderOutcome,
    ReaderRequest,
    ReaderSpec,
    TokenUsage,
    map_decision,
    reader_policy,
)
from .workspace import BoundedEvidenceWorkspace, EvidencePin


class _PhaseFailure(Exception):
    def __init__(self, status: ReaderStatus, reason: str) -> None:
        self.status, self.reason = status, reason


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _render(items: Sequence[RetrievalItem], request: ReaderRequest) -> str:
    return render_evidence_block(
        build_evidence_views(
            items, question=request.question, question_date=request.question_date
        )
    )


class DeliberativeEvidenceReader:
    """Three mandatory phases and at most one shared format-correction call."""

    def __init__(
        self,
        client: ChatClient,
        spec: ReaderSpec,
        *,
        source: EvidenceSource | None = None,
        max_calls: int = 4,
        passage_chars: int = 800,
        protocol: DeliberationProtocol = LITERAL_PROTOCOL,
        execution: DeliberationExecution | None = None,
    ) -> None:
        if spec.prompt_version != protocol.version:
            raise ValueError("deliberative reader requires its own prompt version")
        if type(max_calls) is not int or max_calls not in {3, 4}:
            raise ValueError("max_calls must be 3 or 4")
        if type(passage_chars) is not int or not 256 <= passage_chars <= 1600:
            raise ValueError("passage_chars must be an integer between 256 and 1600")
        self.client, self.spec, self.source = client, spec, source
        self.max_calls, self.passage_chars = max_calls, passage_chars
        self.policy = reader_policy("indexed_evidence")
        self.protocol = protocol
        self.version = protocol.version
        self.execution = execution
        if execution is not None:
            profiles = [execution.primary(p) for p in ("plan", "synthesize", "review")]
            if any(
                call.spec.prompt_version != protocol.version
                for call in (*profiles, execution.recovery)
            ):
                raise ValueError("phase specs must match the deliberation protocol")

    def bind_evidence(self, source: EvidenceSource) -> DeliberativeEvidenceReader:
        return DeliberativeEvidenceReader(
            self.client,
            self.spec,
            source=source,
            max_calls=self.max_calls,
            passage_chars=self.passage_chars,
            protocol=self.protocol,
            execution=self.execution,
        )

    def execution_configuration(self) -> dict[str, Any] | None:
        """Public run identity; no injected transport or credential is serialized."""
        if self.execution is None:
            return None
        return {
            "phases": self.execution.to_dict(),
            "max_calls": self.max_calls,
            "passage_chars": self.passage_chars,
        }

    @staticmethod
    def _quotes(
        pins: Sequence[EvidencePin],
        items: Sequence[RetrievalItem],
        audit: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            {"index": index, "quote": pin.citation.quote}
            for pin in pins
            for index, item in enumerate(items)
            if audit["selected"][index]["document_id"] == pin.document_id
            and audit["selected"][index]["start"] <= pin.start
            and audit["selected"][index]["end"] >= pin.end
            and pin.citation.quote in item.memory.content
        ]

    def generate(self, request: ReaderRequest) -> ReaderOutcome:
        if self.source is None:
            raise ValueError("deliberative reader requires a bound evidence source")
        started = perf_counter()
        workspace = BoundedEvidenceWorkspace(request, passage_chars=self.passage_chars)
        workspace.add(request.items)
        turns: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        inputs: list[int | None] = []
        outputs: list[int | None] = []
        packets: list[dict[str, Any]] = []

        def finish(
            status: ReaderStatus,
            reason: str,
            decision: GroundedAnswerDecision | None = None,
        ) -> ReaderOutcome:
            known = all(type(value) is int and value >= 0 for value in inputs + outputs)
            usage = TokenUsage(
                sum(value for value in inputs if value is not None) if known else None,
                sum(value for value in outputs if value is not None) if known else None,
                "provider" if known else "unknown",
            )
            elapsed = (perf_counter() - started) * 1000
            outcome = (
                map_decision(
                    decision, usage=usage, latency_ms=elapsed, calls=len(turns)
                )
                if decision is not None
                else ReaderOutcome(
                    status,
                    None,
                    reason,
                    error_type=reason,
                    usage=usage,
                    latency_ms=elapsed,
                    calls=len(turns),
                )
            )
            return replace(
                outcome,
                policy_version=self.version,
                prompt_sha256=_digest([t["prompt_sha256"] for t in turns]),
                response_sha256=_digest([t.get("response_sha256") for t in turns]),
                detail=json.dumps(
                    {
                        "version": self.version,
                        "max_calls": self.max_calls,
                        "turns": turns,
                        "actions": actions,
                        "workspace_packets": packets,
                        **(
                            {
                                "execution_policy": self.execution.to_dict(),
                                "execution_policy_sha256": _digest(
                                    self.execution.to_dict()
                                ),
                            }
                            if self.execution is not None
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                ),
            )

        def packet(pins: Sequence[EvidencePin] = ()) -> tuple[RetrievalItem, ...]:
            items, audit = workspace.pack(pins=pins)
            packets.append(audit)
            if audit["pin_errors"]:
                raise _PhaseFailure(ReaderStatus.INVALID, "EVIDENCE_PIN_FAILURE")
            return items

        def phase(
            name: str,
            rules: str,
            items: tuple[RetrievalItem, ...],
            extras: dict[str, Any],
            remaining_phases: int,
        ) -> EvidencePlan | GroundedAnswerDecision:
            validation_error: str | None = None
            failure_reason = f"INVALID_{name.upper()}_OUTPUT"
            while len(turns) < self.max_calls - remaining_phases:
                recovering = validation_error is not None
                call = (
                    self.execution.recovery
                    if self.execution is not None and recovering
                    else self.execution.primary(name)
                    if self.execution is not None
                    else PhaseCall(self.client, self.spec, "legacy")
                )
                payload = {
                    "question": request.question,
                    "question_date": request.question_date,
                    "as_of": request.as_of,
                    "context": _render(items, request),
                    **extras,
                    "validation_error": validation_error,
                }
                messages = [
                    {"role": "system", "content": rules},
                    {
                        "role": "user",
                        "content": json.dumps(
                            payload, ensure_ascii=False, sort_keys=True
                        ),
                    },
                ]
                turn: dict[str, Any] = {
                    "phase": name,
                    "prompt_sha256": _digest(messages),
                    "context": payload["context"],
                    "visible_memory_ids": [item.memory.id for item in items],
                    "workspace_packet": len(packets) - 1,
                }
                turns.append(turn)
                if self.execution is not None:
                    turn.update(
                        call_profile=call.profile,
                        call_spec=call.spec.to_dict(),
                        recovery=recovering,
                    )
                try:
                    response = call.client.complete(
                        model=call.spec.model,
                        messages=messages,
                        temperature=call.spec.temperature,
                        max_tokens=call.spec.max_tokens,
                        timeout_seconds=call.spec.timeout_seconds,
                        response_format={"type": "json_object"},
                    )
                except Exception as exc:  # noqa: BLE001 - fail once; never retry uncertain transport
                    inputs.append(None)
                    outputs.append(None)
                    turn["error"] = type(exc).__name__
                    raise _PhaseFailure(
                        ReaderStatus.TIMEOUT
                        if isinstance(exc, TimeoutError)
                        else ReaderStatus.ERROR,
                        "READER_TRANSPORT_FAILURE",
                    ) from exc
                inputs.append(response.input_tokens)
                outputs.append(response.output_tokens)
                turn["response"] = response.text
                turn["response_sha256"] = sha256(response.text.encode()).hexdigest()
                if self.execution is not None:
                    turn.update(
                        finish_reason=response.finish_reason,
                        reasoning_tokens=response.reasoning_tokens,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        resolved_model=response.model,
                        resolved_revision=response.revision,
                    )
                    problem = completion_problem(response)
                    if problem is not None:
                        failure_reason, recoverable = problem
                        turn["completion_problem"] = failure_reason
                        if not recoverable:
                            raise _PhaseFailure(ReaderStatus.ERROR, failure_reason)
                        # Rebuild from admitted sources, never from a partial response
                        # or hidden reasoning. The recovery shares the format slot.
                        validation_error = (
                            "Previous completion was truncated. Return a complete JSON object."
                            if failure_reason == "OUTPUT_TRUNCATED"
                            else "Previous completion was empty. Return a complete JSON object."
                        )
                        continue
                try:
                    parsed = (
                        self.protocol.parse_plan(response.text, items)
                        if name == "plan"
                        else self.protocol.parse_decision(response.text, items)
                    )
                    turn["citation_indices"] = self.protocol.citation_indices(
                        response.text, name
                    )
                    return parsed
                except (ValueError, TypeError) as exc:
                    validation_error = str(exc)
                    turn["validation_error"] = validation_error
                    failure_reason = f"INVALID_{name.upper()}_OUTPUT"
            raise _PhaseFailure(ReaderStatus.INVALID, failure_reason)

        try:
            initial = packet()
            plan = phase(
                "plan",
                self.protocol.plan_rules,
                initial,
                {
                    "catalog": [
                        {
                            "memory_id": item.memory.id,
                            "displayed_chars": len(item.memory.content),
                        }
                        for item in initial
                    ]
                },
                remaining_phases=2,
            )
            assert isinstance(plan, EvidencePlan)
            notes = workspace.anchor(
                plan.notes, initial, indices=turns[-1]["citation_indices"]
            )
            for query in plan.queries:
                action: dict[str, Any] = {"action": "search", "query": query}
                actions.append(action)
                try:
                    found = tuple(self.source.search(query))
                    selected = [
                        replace(
                            item, memory=replace(item.memory, content=chunk.content)
                        )
                        for item in found
                        for chunk in select_document_chunks(
                            item.memory.content, query, 2 * self.passage_chars
                        )
                    ]
                    action["workspace_add"] = workspace.add(selected)
                    action["returned_memories"] = len(found)
                except Exception as exc:  # noqa: BLE001 - storage failure is not a repairable plan
                    action["error"] = type(exc).__name__
                    raise _PhaseFailure(
                        ReaderStatus.ERROR, "EVIDENCE_SOURCE_FAILURE"
                    ) from exc
            for memory_id, start in plan.reads:
                action = {"action": "read", "memory_id": memory_id, "start": start}
                actions.append(action)
                try:
                    item = self.source.read(memory_id)
                except Exception as exc:  # noqa: BLE001 - preserve source fault classification
                    action["error"] = type(exc).__name__
                    raise _PhaseFailure(
                        ReaderStatus.ERROR, "EVIDENCE_SOURCE_FAILURE"
                    ) from exc
                if item.memory.id != memory_id:
                    action["error"] = "source returned a different memory"
                    raise _PhaseFailure(ReaderStatus.ERROR, "EVIDENCE_SOURCE_FAILURE")
                if start >= len(item.memory.content):
                    action["error"] = "read offset outside stored memory"
                    raise _PhaseFailure(ReaderStatus.INVALID, "INVALID_READ_OFFSET")
                snippet = replace(
                    item,
                    memory=replace(
                        item.memory,
                        content=item.memory.content[
                            start : start + 2 * self.passage_chars
                        ],
                    ),
                )
                action["workspace_add"] = workspace.add((snippet,))
            evidence = packet(notes)
            draft = phase(
                "synthesize",
                self.protocol.answer_rules,
                evidence,
                {"selected_notes": self._quotes(notes, evidence, packets[-1])},
                remaining_phases=1,
            )
            assert isinstance(draft, GroundedAnswerDecision)
            draft_pins = workspace.anchor(
                draft.citations, evidence, indices=turns[-1]["citation_indices"]
            )
            reviewed = packet((*notes, *draft_pins))
            decision = phase(
                "review",
                self.protocol.review_rules,
                reviewed,
                {
                    "selected_notes": self._quotes(notes, reviewed, packets[-1]),
                    "untrusted_draft": {
                        "status": draft.status.value,
                        "answer": draft.answer,
                        "missing": list(draft.missing_requirements),
                        "quoted_premises": self._quotes(
                            draft_pins, reviewed, packets[-1]
                        ),
                    },
                },
                remaining_phases=0,
            )
            assert isinstance(decision, GroundedAnswerDecision)
            return finish(ReaderStatus.INVALID, "INVALID_REVIEW_OUTPUT", decision)
        except _PhaseFailure as exc:
            return finish(exc.status, exc.reason)
