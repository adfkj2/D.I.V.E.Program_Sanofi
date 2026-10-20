"""Bounded, query-independent source windows around admitted personal material."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .episodic_retention import (
    _DETAIL,
    _EVENT,
    _PERSON,
    _UNASSERTED,
    PREDICATE,
    EpisodicRetentionPolicy,
)
from .extraction import Candidate
from .gate import GateAction, GateDecision, _sensitive

_FICTIONAL_SCOPE = re.compile(
    r"\b(?:if|unless|suppose|imagine|hypothetically|fiction\w*|pretend|roleplay|example)\b|"
    r"假如|如果|假设|想象|虚构|举例", re.I)
_NONFACTUAL = re.compile(
    r"\b(?:want\w*|hope\w*|wish\w*|intend\w*|plan\w*|would|could|might|may|should)\b|"
    r"打算|计划|希望", re.I)
_PRECISE_NONFACTUAL = re.compile(
    r"\b(?:want(?:s|ed|ing)?|hop(?:e|es|ed|ing)|wish(?:es|ed|ing)?|"
    r"intend(?:s|ed|ing)?|plan(?:s|ned|ning)?|would|could|might|may|should)\b|"
    r"打算|计划|希望", re.I)
_CLAUSE_BOUNDARY = re.compile(r"[,;，；]|\b(?:because|since|but|so|and)\b", re.I)
_REGULAR_PAST = re.compile(
    r"\b(?:I|we)\s+(?:(?:have|had|just|recently|already)\s+)*[a-z]{3,}ed\b", re.I)

# General grammatical anchors, independent of the reader's question. Keep the
# entire sentence/window: recognizing a clause must not discard its qualifiers.
_REPORTED_SUBJECT = re.compile(
    r"^(?:I|we)\s+(?:(?:have|had|just|recently|already|also|actually|usually|"
    r"normally|currently|now|still|finally)\s+)*(?:[a-z]{3,}ed|"
    r"am|are|was|were|have|had|own|live|work|go|run|use|prefer|like|love|"
    r"bought|got|went|lost|won|met|made|paid|spent|took|read|ran|ate|saw)\b|"
    r"^(?:my|our)\s+(?:[\w'-]+\s+){1,5}(?:is|are|was|were|has|have|"
    r"turned|weighs|measures|costs)\b", re.I)
_DISCOURSE_PREFIX = re.compile(r"^(?:by the way|actually|in fact|as of now)\s*[:,]?\s*", re.I)

# Opt-in precision constraint on NEW detail anchors, not a rewrite of source
# text or a global ban on storing plans. A capitalized destination after "to"
# is not treated as an infinitive ("going to Berlin" remains a reported trip).
_PROSPECTIVE = re.compile(
    r"(?i:\b(?:am|is|are|was|were)\s+(?:\w+ly\s+)?"
    r"(?:about|going|scheduled|set)\s+to\s+)(?=[a-z])|"
    r"(?i:\b(?:am|is|are|was|were)\s+(?:\w+ly\s+)?(?:hoping|wishing)\b)")


def _has_reported_detail(sentence, *, assertions_only=False, precise_terms=False):
    if _FICTIONAL_SCOPE.search(sentence):
        return False
    clauses = _CLAUSE_BOUNDARY.split(sentence)
    if sentence.endswith(("?", "？")):
        # A question itself is not an assertion. A preceding, independently
        # declarative clause can anchor its original context ("I ..., how ...?").
        clauses = clauses[:-1]
    for clause in clauses:
        clause = _DISCOURSE_PREFIX.sub("", clause.strip())
        clause = re.sub(r"\bI'm\b", "I am", clause, flags=re.I)
        clause = re.sub(r"\b(I|we)['’]ve\b", r"\1 have", clause, flags=re.I)
        modality = _PRECISE_NONFACTUAL if precise_terms else _NONFACTUAL
        if modality.search(clause) or not _REPORTED_SUBJECT.search(clause):
            continue
        if assertions_only and _PROSPECTIVE.search(clause):
            continue
        if _DETAIL.search(clause) or re.search(r"\b[A-Z][a-z]{2,}\b", clause[1:]):
            return True
    return False


def _has_asserted_personal_clause(sentence):
    """Find an independently stated episode, without stripping its modality.

    A conditional/fictional scope or a question is never split into an assertion.
    In ordinary mixed discourse, only a clause with its own personal subject,
    event and concrete detail can anchor a window. The stored window is still
    verbatim OBSERVATION, including any adjacent intention or uncertainty.
    """
    if sentence.endswith(("?", "？")) or _FICTIONAL_SCOPE.search(sentence):
        return False
    for clause in _CLAUSE_BOUNDARY.split(sentence):
        if _NONFACTUAL.search(clause) or not _PERSON.search(clause):
            continue
        if not (_EVENT.search(clause) or _REGULAR_PAST.search(clause)):
            continue
        named = re.search(r"\b[A-Z][a-z]{2,}\b", clause.strip()[1:])
        if _DETAIL.search(clause) or named:
            return True
    return False


@dataclass(frozen=True)
class NarrativeRetentionPolicy:
    gate: object
    max_items: int = 4
    max_window_chars: int = 1800
    max_turn_chars: int = 8000
    neighbor_sentences: int = 1
    clause_anchors: bool = False
    reported_detail_anchors: bool = False
    reported_detail_assertions_only: bool = False
    precise_detail_terms: bool = False

    def __post_init__(self):
        if self.reported_detail_assertions_only and not self.reported_detail_anchors:
            raise ValueError("assertion constraint requires reported detail anchors")
        if self.precise_detail_terms and not self.reported_detail_anchors:
            raise ValueError("precise detail terms require reported detail anchors")
        if min(self.max_items, self.max_window_chars, self.max_turn_chars) <= 0:
            raise ValueError("narrative limits must be positive")
        if not 0 <= self.neighbor_sentences <= 2:
            raise ValueError("narrative neighbor radius must be between zero and two")

    def candidates(self, text, *, source_type, observed_at, existing):
        if source_type != "user" or len(text) > self.max_turn_chars or _sensitive(text):
            return []
        sentences = [(start, sentence) for start, sentence in EpisodicRetentionPolicy._sentences(text)
                     if sentence]
        ordinary = EpisodicRetentionPolicy().candidates(
            text, source_type=source_type, observed_at=observed_at, existing=[])
        spans = [tuple(c.structured_content["source_span"]) for c in ordinary]
        kwargs = {"source_type": source_type} if getattr(self.gate, "supports_source_type", False) else {}
        decision = self.gate(text, explicit=False, **kwargs)
        admitted = bool(existing) or (decision.accepted and decision.action == GateAction.WRITE)
        anchors = []
        for i, (start, sentence) in enumerate(sentences):
            end = start + len(sentence)
            if self.reported_detail_anchors and _has_reported_detail(
                    sentence, assertions_only=self.reported_detail_assertions_only,
                    precise_terms=self.precise_detail_terms):
                anchors.append(i)
                continue
            if self.clause_anchors:
                if sentence.endswith(("?", "？")) or _FICTIONAL_SCOPE.search(sentence):
                    continue
                if _has_asserted_personal_clause(sentence):
                    anchors.append(i)
                    continue
                if _NONFACTUAL.search(sentence):
                    continue
            if sentence.endswith(("?", "？")) or _UNASSERTED.search(sentence):
                continue
            if (start, end) in spans or (admitted and _PERSON.search(sentence) and len(sentence) >= 20):
                anchors.append(i)
        windows = []
        for i in anchors:
            lo = hi = i
            if len(sentences[i][1]) > self.max_window_chars:
                continue  # Never truncate an assertion into a different one.
            for distance in range(1, self.neighbor_sentences + 1):
                for candidate in (i - distance, i + distance):
                    if not 0 <= candidate < len(sentences):
                        continue
                    left, right = min(lo, candidate), max(hi, candidate)
                    a = sentences[left][0]
                    b = sentences[right][0] + len(sentences[right][1])
                    if b - a <= self.max_window_chars:
                        lo, hi = left, right
            a = sentences[lo][0]
            b = sentences[hi][0] + len(sentences[hi][1])
            if windows and a <= windows[-1][1] and b - windows[-1][0] <= self.max_window_chars:
                windows[-1] = (windows[-1][0], max(b, windows[-1][1]))
            elif not any(a >= x and b <= y for x, y in windows):
                windows.append((a, b))
        result = []
        version = "selective-narrative-clauses-v1" if self.clause_anchors else "selective-narrative-v1"
        if self.reported_detail_anchors:
            version = "selective-narrative-reported-details-v1"
        if self.reported_detail_assertions_only:
            version = "selective-narrative-asserted-details-v1"
        if self.precise_detail_terms:
            version = "selective-narrative-precise-detail-terms-v1"
        for start, end in windows[:self.max_items]:
            content = text[start:end]
            if any(content == c.content for c in existing):
                continue
            gate = GateDecision(True, .55, .8, .6, "medium_term",
                "source context around selectively admitted personal material", GateAction.WRITE,
                "NARRATIVE_OBSERVATION", {"narrative_retention": 1.0}, version)
            result.append(Candidate(content, "episode", "OBSERVATION", {
                "subject": "user", "predicate": PREDICATE, "assertion": "reported_context",
                "role": "user", "source_span": [start, end],
                "retention_policy": version,
                "context_may_include_questions_or_hypotheses": True,
            }, gate, observed_at))
        return result
