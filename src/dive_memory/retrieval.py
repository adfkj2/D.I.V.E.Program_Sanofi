"""Retrieval configuration: which channels run, and how results are re-ranked.

Two orthogonal knobs, deliberately separated because they fail differently:

* :class:`RetrievalConfig` -- *which channels exist and how deep they dig*. A
  channel that is off cannot contribute a candidate at all, so this decides
  **recall**.
* :class:`ScoringPolicy` -- *how the candidates that were found are ordered on
  top of reciprocal-rank fusion*. A policy cannot recover a candidate the
  channels never returned, so this decides **precision at k**.

Every component of the re-rank is named and independently weighted, and
``ScoringPolicy.legacy()`` reproduces the pre-policy formula
(``quality = 0.8 + 0.1*importance + 0.1*confidence``) exactly, so every published
number stays reproducible and an ablation can move one component at a time.

The one component that exists for a measured reason rather than for symmetry is
``document_weight``: once the write gate admits long non-trusted deliverables as
document memories, those memories are both numerous and long, so a
recency/importance re-rank alone would let them crowd short scalar facts out of a
ten-slot result list. ``document_weight`` and ``document_char_budget`` are the two
place where that is controlled, and both default to leaving the retrieved text
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Mapping


RETRIEVAL_CHANNELS = frozenset({
    "bm25", "dense", "predicate", "temporal", "entity", "relation", "multi_hop",
    "document_chunk",
})
# The passage index is a new, opt-in experiment.  Historical full/ablation
# configurations must keep exactly the channels that produced their reports.
BASELINE_RETRIEVAL_CHANNELS = RETRIEVAL_CHANNELS - {"document_chunk"}

#: Components of the re-rank score, in the order the docstring of
#: :class:`ScoringPolicy` describes them.
SCORING_COMPONENTS = (
    "semantic", "keyword", "recency", "importance", "temporal_compatibility", "memory_state",
)


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """How retrieved candidates are ordered on top of reciprocal-rank fusion.

    The score is a product of a *quality* term and the fusion term, so the
    re-rank can never invert the fusion wholesale: it breaks ties and reorders
    within a band. ``legacy()`` is the exact pre-policy formula.

    Components, each independently weighted so an ablation can move one:

    ``semantic``
        The normalised dense (embedding) score of the candidate. Only meaningful
        when the store's embedder is semantic; with the deterministic stub the
        dense channel is filtered out before fusion and this term is zero.
    ``keyword``
        The normalised lexical score of the candidate.
    ``recency``
        A half-life decay on the candidate's anchor instant, relative to the
        query's ``as_of``. This is the only component that can express "the more
        recent statement wins" without a model.
    ``importance`` / ``confidence``
        The write-time scores the candidate carries. These are the two terms the
        legacy formula used.
    ``temporal_compatibility``
        Whether the candidate's validity window covers the query instant. A
        statement that had already been closed at ``as_of`` is penalised rather
        than dropped, so the reader can still see it when the question asks for
        history.
    ``memory_state``
        A multiplier for lifecycle state (current vs superseded vs archived).
        Applied multiplicatively because it is a *veto-shaped* signal: a
        superseded memory should fall behind a current one at any rank, not by a
        constant amount.
    ``document_weight``
        A multiplier for document memories (long non-trusted deliverables). It is
        applied multiplicatively for the same reason as ``memory_state``.
    """

    # -- additive quality terms -------------------------------------------- #
    base_quality: float = 0.8
    importance_weight: float = 0.1
    confidence_weight: float = 0.1
    semantic_weight: float = 0.0
    keyword_weight: float = 0.0
    recency_weight: float = 0.0
    temporal_weight: float = 0.0
    # -- multiplicative terms ---------------------------------------------- #
    superseded_factor: float = 1.0
    archived_factor: float = 1.0
    document_weight: float = 1.0
    # -- parameters -------------------------------------------------------- #
    recency_half_life_days: float = 180.0
    #: Character ceiling for a single document memory once context is packed.
    #: ``0`` means no ceiling. It lives on the scoring policy because it is part
    #: of the same decision: how much of the reader's attention one document may
    #: take.
    document_char_budget: int = 0
    version: str = "scoring-policy-v1"
    name: str = "legacy"

    def __post_init__(self) -> None:
        for label in ("base_quality", "importance_weight", "confidence_weight",
                      "semantic_weight", "keyword_weight", "recency_weight",
                      "temporal_weight"):
            value = float(getattr(self, label))
            if value < 0:
                raise ValueError(f"{label} must not be negative")
        for label in ("superseded_factor", "archived_factor", "document_weight"):
            value = float(getattr(self, label))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{label} must be within [0, 1]")
        if self.recency_half_life_days <= 0:
            raise ValueError("recency_half_life_days must be positive")
        if self.document_char_budget < 0:
            raise ValueError("document_char_budget must not be negative")

    # -- named policies ---------------------------------------------------- #

    @classmethod
    def legacy(cls) -> "ScoringPolicy":
        """The formula every published artefact was produced with."""

        return cls()

    @classmethod
    def balanced(cls) -> "ScoringPolicy":
        """Legacy plus recency, temporal compatibility and a document damper.

        Every added term is one the corpus can justify: an update question needs
        the newer of two statements, a temporal question needs the statement
        whose window covers the asked instant, and a document damper is needed
        because documents are 2,166 characters on average against a 1,500-token
        context budget.
        """

        return cls(
            semantic_weight=0.08,
            keyword_weight=0.04,
            recency_weight=0.10,
            temporal_weight=0.08,
            superseded_factor=0.6,
            archived_factor=0.8,
            document_weight=0.85,
            document_char_budget=1200,
            name="balanced",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "components": {
                "semantic": self.semantic_weight,
                "keyword": self.keyword_weight,
                "recency": self.recency_weight,
                "importance": self.importance_weight,
                "temporal_compatibility": self.temporal_weight,
                "memory_state": {
                    "superseded_factor": self.superseded_factor,
                    "archived_factor": self.archived_factor,
                },
            },
            "base_quality": self.base_quality,
            "confidence_weight": self.confidence_weight,
            "recency_half_life_days": self.recency_half_life_days,
            "document_weight": self.document_weight,
            "document_char_budget": self.document_char_budget,
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()


SCORING_POLICIES: dict[str, ScoringPolicy] = {
    "legacy": ScoringPolicy.legacy(),
    "balanced": ScoringPolicy.balanced(),
}


def scoring_policy(name: str) -> ScoringPolicy:
    """Look a scoring policy up by name, failing loudly on a typo."""

    try:
        return SCORING_POLICIES[name]
    except KeyError:
        raise ValueError(
            f"unknown scoring policy {name!r}; available: {sorted(SCORING_POLICIES)!r}"
        ) from None


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    enabled_channels: frozenset[str] = frozenset({
        "bm25", "dense", "predicate", "temporal", "entity", "relation",
    })
    candidate_depths: Mapping[str, int] = field(default_factory=dict)
    rrf_k: int = 60
    channel_weights: Mapping[str, float] = field(default_factory=dict)
    scoring: ScoringPolicy = field(default_factory=ScoringPolicy.legacy)
    version: str = "retrieval-config-v1"
    name: str = "default"

    def __post_init__(self) -> None:
        unknown = set(self.enabled_channels) - RETRIEVAL_CHANNELS
        if unknown:
            raise ValueError(f"unknown retrieval channels: {sorted(unknown)!r}")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        for channel, depth in self.candidate_depths.items():
            if channel not in RETRIEVAL_CHANNELS or isinstance(depth, bool) or int(depth) <= 0:
                raise ValueError("candidate depths require known channels and positive integers")
        for channel, weight in self.channel_weights.items():
            if channel not in RETRIEVAL_CHANNELS or float(weight) < 0:
                raise ValueError("channel weights require known channels and non-negative values")

    def depth(self, channel: str) -> int:
        return int(self.candidate_depths.get(channel, 100))

    def weight(self, channel: str) -> float:
        return float(self.channel_weights.get(channel, 1.0))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "enabled_channels": sorted(self.enabled_channels),
            "rrf_k": self.rrf_k,
            "candidate_depths": dict(self.candidate_depths),
            "channel_weights": dict(self.channel_weights),
            "scoring": self.scoring.to_dict(),
            "scoring_fingerprint": self.scoring.fingerprint(),
        }

    @classmethod
    def full(cls, *, include_multi_hop: bool = False, include_document_chunks: bool = False,
             name: str = "full",
             scoring: ScoringPolicy | None = None) -> "RetrievalConfig":
        channels = set(BASELINE_RETRIEVAL_CHANNELS)
        if not include_multi_hop:
            channels.remove("multi_hop")
        if include_document_chunks:
            channels.add("document_chunk")
        kwargs = {"scoring": scoring} if scoring is not None else {}
        return cls(frozenset(channels), name=name, **kwargs)


def ablation_configs() -> dict[str, RetrievalConfig]:
    variants = {
        "A": {"bm25"},
        "B": {"dense"},
        "C": {"bm25", "dense"},
        "D": {"bm25", "dense", "predicate"},
        "E": {"bm25", "dense", "predicate", "temporal"},
        "F": {"bm25", "dense", "predicate", "temporal", "entity", "relation"},
        "G": set(BASELINE_RETRIEVAL_CHANNELS),
        "H": set(BASELINE_RETRIEVAL_CHANNELS),
    }
    return {name: RetrievalConfig(frozenset(channels), name=name) for name, channels in variants.items()}
