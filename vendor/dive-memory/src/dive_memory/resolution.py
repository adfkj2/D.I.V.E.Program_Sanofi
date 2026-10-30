from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .models import Memory
from .normalization import normalize_text
from .relations import is_single_valued
from .temporal import parse_instant


class MemoryRelationship(StrEnum):
    UNRELATED = "unrelated"
    DUPLICATE = "duplicate"
    REINFORCEMENT = "reinforcement"
    REFINEMENT = "refinement"
    CORRECTION = "correction"
    TEMPORAL_UPDATE = "temporal_update"
    CONTRADICTION = "contradiction"
    SUPERSESSION = "supersession"


class ResolutionAction(StrEnum):
    CREATE = "CREATE"
    MERGE_PROVENANCE = "MERGE_PROVENANCE"
    REINFORCE = "REINFORCE"
    SUPERSEDE = "SUPERSEDE"
    COEXIST = "COEXIST"


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    event_type: str
    source_text: str
    forced: bool = False
    # Provenance of the *candidate* (the new text). ``None`` means the caller
    # predates v2 and the pre-existing behaviour applies unchanged.
    source_type: str | None = None
    # Provenance of the *existing* memory, when the store can supply it.
    existing_source_type: str | None = None


@dataclass(frozen=True, slots=True)
class ResolutionProposal:
    relationship: MemoryRelationship
    action: ResolutionAction
    confidence: float
    reason: str
    resolver_version: str = "relationship-rules-v1"


_REINFORCEMENT = ("again", "confirm", "still", "as before", "再次", "仍然", "确认", "还是")
_CORRECTION = ("correction", "correct that", "actually", "更正", "纠正", "说错了", "其实")
_TEMPORAL = ("moved", "move to", "now live", "搬到", "迁到", "现在住")
_CONTRADICTION = ("not ", "no longer", "isn't", "do not", "不是", "不再", "并非", "没有")
_REFINEMENT = ("more specifically", "to be precise", "具体", "准确地说", "更确切")

# Provenance tiers that may never close out an existing memory on their own.
# The false-memory suite's ``sd-summary-overwrites-user`` case is exactly this:
# a generated summary restating "我住在北京" is a well-formed, high-confidence
# single-valued fact, so the v1 rules supersede the directly supplied
# "我住在成都". Content alone cannot express "the user never said this".
_DERIVED_SOURCES = frozenset({
    "summary", "generated", "agent", "tool", "assistant",
})
# Provenance tiers whose facts the user is personally accountable for.
_AUTHORITATIVE_SOURCES = frozenset({"user", "memory_correction", "user_correction"})


def _is_derived(source_type: str | None) -> bool:
    return source_type is not None and str(source_type).strip().casefold() in _DERIVED_SOURCES


def _is_authoritative(source_type: str | None) -> bool:
    return source_type is not None and str(source_type).strip().casefold() in _AUTHORITATIVE_SOURCES


class RelationshipResolver:
    version = "relationship-rules-v1"

    @staticmethod
    def _identity(memory: Memory) -> tuple[str, str]:
        subject = normalize_text(str(memory.structured_content.get("subject", "user")), casefold=True)
        predicate = normalize_text(str(memory.structured_content.get("predicate", "statement")), casefold=True)
        return subject, predicate

    @staticmethod
    def _value(memory: Memory) -> str:
        value = memory.structured_content.get("normalized_value", memory.structured_content.get("value", ""))
        return normalize_text(str(value), casefold=True)

    def find_related(self, candidate: Memory, existing: Iterable[Memory]) -> Memory | None:
        identity = self._identity(candidate)
        related = [memory for memory in existing if self._identity(memory) == identity]
        if not related:
            return None
        candidate_value = self._value(candidate)
        exact = [memory for memory in related if self._value(memory) == candidate_value]
        return (exact or related)[-1]

    def classify(self, existing: Memory, candidate: Memory,
                 context: ResolutionContext) -> ResolutionProposal:
        text = normalize_text(context.source_text, casefold=True)
        if context.forced or context.event_type == "memory_correction" or any(mark in text for mark in _CORRECTION):
            return ResolutionProposal(MemoryRelationship.CORRECTION, ResolutionAction.SUPERSEDE, 1.0,
                                      "explicit correction targets an existing memory")
        if self._identity(existing) != self._identity(candidate):
            return ResolutionProposal(MemoryRelationship.UNRELATED, ResolutionAction.CREATE, 1.0,
                                      "subject or predicate differs")

        # --- provenance guard -------------------------------------------------
        # A derived source (summary / generated / tool / assistant) may only
        # *corroborate* an authoritative fact. It must never close out a
        # memory the user supplied directly, even when it is a confident,
        # single-valued, later-dated statement. Checked before the value
        # comparison so a derived restatement also cannot take the REFINEMENT
        # branch, and before the temporal branch so an older user fact is not
        # silently superseded by a newer machine paraphrase.
        if _is_derived(context.source_type) and _is_authoritative(context.existing_source_type):
            if self._value(existing) == self._value(candidate):
                return ResolutionProposal(
                    MemoryRelationship.REINFORCEMENT, ResolutionAction.MERGE_PROVENANCE, 0.9,
                    "derived source corroborates an authoritative fact without changing it")
            return ResolutionProposal(
                MemoryRelationship.CONTRADICTION, ResolutionAction.COEXIST, 0.9,
                "derived source cannot supersede an authoritative fact; values recorded as conflicting")

        old_value = self._value(existing)
        new_value = self._value(candidate)
        if old_value == new_value:
            if any(mark in text for mark in _REINFORCEMENT):
                return ResolutionProposal(MemoryRelationship.REINFORCEMENT, ResolutionAction.REINFORCE, 0.95,
                                          "same normalized fact with explicit independent confirmation")
            return ResolutionProposal(MemoryRelationship.DUPLICATE, ResolutionAction.MERGE_PROVENANCE, 1.0,
                                      "same normalized subject, predicate, and value")

        if any(mark in text for mark in _REFINEMENT) or (
            (old_value in new_value or new_value in old_value) and len(old_value) != len(new_value)
        ):
            return ResolutionProposal(MemoryRelationship.REFINEMENT, ResolutionAction.SUPERSEDE, 0.85,
                                      "candidate narrows or expands the previous value")

        predicate = self._identity(candidate)[1]
        old_time = parse_instant(existing.valid_from)
        new_time = parse_instant(candidate.valid_from)
        if is_single_valued(predicate) and (
            any(mark in text for mark in _TEMPORAL) or
            (old_time is not None and new_time is not None and new_time > old_time)
        ):
            return ResolutionProposal(MemoryRelationship.TEMPORAL_UPDATE, ResolutionAction.SUPERSEDE, 0.95,
                                      "new single-valued fact starts after the previous fact")

        if any(mark in text for mark in _CONTRADICTION) or candidate.confidence < 0.6:
            return ResolutionProposal(MemoryRelationship.CONTRADICTION, ResolutionAction.COEXIST, 0.75,
                                      "conflicting value lacks safe supersession evidence")
        if is_single_valued(predicate):
            return ResolutionProposal(MemoryRelationship.SUPERSESSION, ResolutionAction.SUPERSEDE, 0.9,
                                      "newer high-confidence value replaces a single-valued predicate")
        return ResolutionProposal(MemoryRelationship.UNRELATED, ResolutionAction.CREATE, 0.9,
                                  "multi-valued predicate permits coexisting values")
