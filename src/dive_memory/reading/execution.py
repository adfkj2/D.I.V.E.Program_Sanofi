"""Explicit, provider-neutral phase routing and completion boundaries.

Profiles hold injected transports, never credentials or provider-specific options.
Their public description binds model specs and budgets without serializing clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .reader import ChatClient, ChatCompletion, ReaderSpec


@dataclass(frozen=True, slots=True)
class PhaseCall:
    client: ChatClient
    spec: ReaderSpec
    profile: str
    reasoning_effort: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "spec": self.spec.to_dict(),
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class DeliberationExecution:
    """One primary per phase and one shared recovery, within the reader call cap."""

    plan: PhaseCall
    synthesize: PhaseCall
    review: PhaseCall
    recovery: PhaseCall
    version: str = "staged-completion-v1"

    def primary(self, phase: str) -> PhaseCall:
        if phase == "plan":
            return self.plan
        if phase == "synthesize":
            return self.synthesize
        if phase == "review":
            return self.review
        raise ValueError("unknown deliberation phase")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            **{
                name: self.primary(name).to_dict()
                for name in ("plan", "synthesize", "review")
            },
            "recovery": self.recovery.to_dict(),
        }


def completion_problem(response: ChatCompletion) -> tuple[str, bool] | None:
    """Return a stable reason and whether a known-cost completion is recoverable.

    Length takes precedence over JSON validity: even a parseable prefix was not
    certified complete. Missing reasoning details do not make total usage unknown.
    No inference of a finish reason is made from token counts alone.
    """
    if any(
        type(value) is not int or value < 0
        for value in (response.input_tokens, response.output_tokens)
    ):
        return "READER_USAGE_UNKNOWN", False
    if response.finish_reason == "length":
        return "OUTPUT_TRUNCATED", True
    if response.finish_reason == "content_filter":
        return "READER_CONTENT_FILTERED", False
    if response.finish_reason not in {None, "stop"}:
        return "READER_UNEXPECTED_FINISH", False
    if not response.text.strip():
        return "EMPTY_OUTPUT", True
    return None
