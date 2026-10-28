"""Bounded user-turn observations, independent of semantic-fact promotion.

Only explicitly injected policies use this layer. It consumes the current
admitted turn, never a query, answer key, conversation archive or raw-store read.
Plans and questions retain their exact modality as observations, not user facts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from .extraction import Candidate
from .gate import GateAction, GateDecision, _sensitive

VERSION = "bounded-dialogue-observation-v1"
PREDICATE = "dialogue_observation"
SOURCE_GUARD = "dialogue_source_guard"
USER_SOURCE_TYPES = frozenset({"user", "human", "user_correction", "memory_correction"})
_OPT_OUT = re.compile(
    r"\b(?:do\s+not|don['’]t|never)\s+(?:remember|store|save|retain|keep)\b|"
    r"\b(?:not\s+for\s+(?:your\s+)?memory|off\s+the\s+record)\b|"
    r"不要(?:记住|记录|保存|存储)|别(?:记住|记录|保存|存储)|请勿(?:记住|记录|保存|存储)",
    re.I,
)


def is_dialogue_observation(structured: Mapping[str, Any]) -> bool:
    """Recognize this opt-in layer, including deduplicated composed windows."""
    return (
        structured.get("predicate") == PREDICATE
        or structured.get(SOURCE_GUARD) == VERSION
    )


def _is_user_source(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().casefold() in USER_SOURCE_TYPES


@dataclass(frozen=True, slots=True)
class DialogueRetentionPolicy:
    """Preserve one exact bounded observation after ordinary extraction.

    ``base`` can compose the existing episode policy. Full user observations
    already produced by either layer suppress the extra copy. The extraction
    provider must honor ``blocks_turn`` before producing ordinary candidates,
    and the store/service must enforce same-source forgetting before this
    opt-in is enabled; a stateless policy cannot enforce durable tombstones.
    """

    base: Any | None = None
    max_chars: int = 2000
    max_turn_chars: int = 16000
    version: ClassVar[str] = VERSION

    def __post_init__(self) -> None:
        if (
            type(self.max_chars) is not int
            or type(self.max_turn_chars) is not int
            or not 0 < self.max_chars <= self.max_turn_chars
        ):
            raise ValueError("dialogue retention bounds must be positive integers")

    @staticmethod
    def blocks_turn(text: str, *, source_type: str | None) -> bool:
        """Check the entire turn so a clipped-out secret cannot bypass consent."""
        return _is_user_source(source_type) and bool(
            _sensitive(text) or _OPT_OUT.search(text)
        )

    @staticmethod
    def protect_candidates(
        candidates: Iterable[Candidate], *, source_type: str | None
    ) -> list[Candidate]:
        """Preserve forgetting when a composed observation suppresses a copy.

        Only observations under this explicitly installed policy are marked.
        Semantic facts, source text and caller-owned candidate objects stay
        unchanged. Partial context windows need the same conservative guard:
        they may also contain the fact a user subsequently asks to forget.
        """
        return [
            replace(
                candidate,
                structured_content={
                    **candidate.structured_content,
                    SOURCE_GUARD: VERSION,
                },
            )
            if _is_user_source(source_type)
            and candidate.evidence_state == "OBSERVATION"
            and candidate.structured_content.get("role") == "user"
            else candidate
            for candidate in candidates
        ]

    def candidates(
        self,
        text: str,
        *,
        source_type: str | None,
        observed_at: str | None,
        existing: list[Candidate],
    ) -> list[Candidate]:
        if self.blocks_turn(text, source_type=source_type):
            return []
        extra = (
            list(
                self.base.candidates(
                    text,
                    source_type=source_type,
                    observed_at=observed_at,
                    existing=existing,
                )
            )
            if self.base is not None
            else []
        )
        if (
            not _is_user_source(source_type)
            or len(text) > self.max_turn_chars
            or not text.strip()
        ):
            return extra
        complete = text.strip()
        if any(
            candidate.evidence_state == "OBSERVATION"
            and candidate.structured_content.get("role") == "user"
            and complete in candidate.content
            for candidate in (*existing, *extra)
        ):
            return extra
        start = len(text) - len(text.lstrip())
        original_end = len(text.rstrip())
        end = min(original_end, start + self.max_chars)
        content = text[start:end]
        decision = GateDecision(
            True,
            0.45,
            0.8,
            0.5,
            "medium_term",
            "admitted user turn retained verbatim as an observation",
            GateAction.WRITE,
            "DIALOGUE_OBSERVATION",
            {"dialogue_retention": 1.0},
            VERSION,
        )
        extra.append(
            Candidate(
                content,
                "episode",
                "OBSERVATION",
                {
                    "subject": "user",
                    "predicate": PREDICATE,
                    "assertion": "reported_dialogue",
                    "role": "user",
                    "source_span": [start, end],
                    "source_chars": len(text),
                    "retained_chars": len(content),
                    "truncated": end < original_end,
                    "retention_policy": VERSION,
                },
                decision,
                observed_at,
            )
        )
        return extra
