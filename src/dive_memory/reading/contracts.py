"""Shared reader outcomes and boundary error, with no benchmark inputs."""

from enum import StrEnum

CANONICAL_ABSTENTION_HYPOTHESIS = (
    "The available conversation history does not contain enough information "
    "to answer this question."
)


class LeakageError(RuntimeError):
    """Raised when a gold-only field reaches a writer-visible surface."""


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
