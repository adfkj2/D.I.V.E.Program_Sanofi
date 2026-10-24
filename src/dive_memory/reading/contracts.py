"""Shared reader outcomes and boundary error, with no benchmark inputs."""

from enum import StrEnum
from typing import Protocol, Sequence

from ..models import RetrievalItem

CANONICAL_ABSTENTION_HYPOTHESIS = (
    "The available conversation history does not contain enough information "
    "to answer this question."
)


class LeakageError(RuntimeError):
    """Raised when a gold-only field reaches a writer-visible surface."""


class EvidenceSource(Protocol):
    """Read-only access to already admitted, caller-scoped memories."""

    def search(self, query: str) -> Sequence[RetrievalItem]: ...

    def read(self, memory_id: str) -> RetrievalItem: ...


class ReaderStatus(StrEnum):
    """Outcome of one reader call, kept distinct from a semantic abstention."""

    ANSWER = "ANSWER"
    ABSTAIN = "ABSTAIN"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    INVALID = "INVALID"


class HypothesisOrigin(StrEnum):
    READER = "READER"
    READER_ABSTENTION = "READER_ABSTENTION"
    ERROR_FALLBACK = "ERROR_FALLBACK"
