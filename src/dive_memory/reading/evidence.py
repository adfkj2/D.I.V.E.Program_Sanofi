"""Deterministic evidence preparation for the Oracle reader.

The v1 reader was handed a flat, undated, role-free block of memory text and a
single instruction: *answer only when every premise is explicitly supported*.
That instruction is correct for a fact lookup and wrong for everything a
long-term memory system is actually asked to do -- combine two sessions, notice
that a fact changed, subtract two dates, or aggregate preferences. The reader
had no way to see any of that structure, so it refused.

This module builds the structure. It is deliberately **pure and offline**: no
model call, no network, no benchmark knowledge. Everything here is a function of
the retrieved items and the question.

Four jobs:

1. **Normalise** each retrieved memory into an :class:`EvidenceView` carrying the
   fields a reader needs but the store keeps in columns the prompt never saw:
   validity window, lifecycle state, speaker role, distance from the question
   date, and a relevance proxy.
2. **Classify** the retrieval set into one of four states --
   ``NO_EVIDENCE`` / ``WEAK_EVIDENCE`` / ``CONFLICTING_EVIDENCE`` /
   ``SUFFICIENT_EVIDENCE`` -- so "refuse" becomes a decision with a reason
   instead of a default.
3. **Render** the annotated evidence block that replaces the flat context.
4. **Scaffold** the temporal question: which answer shape is being asked for,
   and what the dated evidence actually spans.

Two honesty notes, both carried into the artefacts:

* ``relevance`` is a **lexical proxy** (content-word overlap plus a retrieval
  channel prior), not a similarity score. With a non-semantic embedder in the
  store it is the only offline signal available, and it is labelled as a proxy
  everywhere it surfaces.
* ``answer_kind`` is a question-shape classifier, not an intent understanding.
  It exists to select a reasoning instruction, never to alter retrieval or to
  write an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Sequence

from ..models import DOCUMENT_ASSERTION, DOCUMENT_PREDICATE, MemoryStatus, RetrievalItem

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


class EvidenceState(StrEnum):
    """How much usable evidence the reader has."""

    NO_EVIDENCE = "NO_EVIDENCE"
    WEAK_EVIDENCE = "WEAK_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    SUFFICIENT_EVIDENCE = "SUFFICIENT_EVIDENCE"


class LifecycleState(StrEnum):
    """The lifecycle state a reader needs to reason about updates."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    REMOVED = "REMOVED"

    @classmethod
    def of(cls, status: MemoryStatus | str) -> "LifecycleState":
        value = getattr(status, "value", status)
        if value in {MemoryStatus.SUPERSEDED.value}:
            return cls.SUPERSEDED
        if value in {MemoryStatus.ARCHIVED.value}:
            return cls.ARCHIVED
        if value in {MemoryStatus.DELETED.value, MemoryStatus.MERGED.value}:
            return cls.REMOVED
        return cls.CURRENT


class AnswerKind(StrEnum):
    """The *shape* of answer the question asks for.

    Used only to select which reasoning instruction the reader is given.
    """

    TIME_POINT = "time_point"
    DURATION = "duration"
    ORDERING = "ordering"
    COUNT = "count"
    LIST = "list"
    PREFERENCE = "preference"
    ASSISTANT_RECALL = "assistant_recall"
    FACT = "fact"
    UNKNOWN = "unknown"


#: Words that carry no topical signal. Kept short and bilingual; a longer list
#: would be a tuning surface with no test behind it.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "am",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "i",
        "me",
        "my",
        "mine",
        "we",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "many",
        "much",
        "long",
        "often",
        "did",
        "was",
        "s",
        "t",
        "re",
        "ve",
        "ll",
        "d",
        "m",
        "的",
        "了",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "和",
        "与",
        "或",
        "什么",
        "哪",
        "哪里",
        "怎么",
        "如何",
        "多少",
        "几",
    }
)

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|[\u4e00-\u9fff]")


def content_tokens(text: str) -> list[str]:
    """Lowercased content words. CJK is tokenised per character."""

    return [
        token
        for token in _TOKEN.findall(str(text).casefold())
        if token not in _STOPWORDS
    ]


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """One retrieved memory, described the way a reader needs to see it."""

    index: int
    memory_id: str
    content: str
    source_refs: tuple[str, ...]
    channels: tuple[str, ...]
    retrieval_score: float
    valid_from: str | None
    valid_to: str | None
    observed_at: str | None
    lifecycle: LifecycleState
    role: str
    session_date: str | None
    days_before_question: int | None
    relevance: float
    conflict_group: str | None = None
    source_positions: tuple[dict[str, Any], ...] = ()
    statement_sources: tuple[dict[str, Any], ...] = ()

    @property
    def is_current(self) -> bool:
        return self.lifecycle is LifecycleState.CURRENT

    def to_dict(self) -> dict[str, Any]:
        result = {
            "index": self.index,
            "memory_id": self.memory_id,
            "source_refs": list(self.source_refs),
            "channels": list(self.channels),
            "retrieval_score": round(self.retrieval_score, 6),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "lifecycle": self.lifecycle.value,
            "role": self.role,
            "days_before_question": self.days_before_question,
            "relevance_proxy": round(self.relevance, 4),
            "conflict_group": self.conflict_group,
        }
        if self.source_positions:
            result["source_positions"] = list(self.source_positions)
        if self.statement_sources:
            result["statement_sources"] = list(self.statement_sources)
            result["observed_at"] = self.observed_at
        return result


#: Retrieval channels that only ever match on wording or on a typed predicate.
#: A memory that came back through one of these is *about* the words in the
#: question, which is a weak but real relevance signal.
LEXICAL_CHANNELS = frozenset({"bm25", "predicate", "entity", "relation"})

#: Relevance weights. Both terms are proxies; the split is documented so a
#: report can say which one moved.
_OVERLAP_WEIGHT = 0.8
_CHANNEL_WEIGHT = 0.2

SUFFICIENT_RELEVANCE = 0.20
WEAK_RELEVANCE = 0.06


def relevance_proxy(question: str, content: str, channels: Iterable[str]) -> float:
    """Content-word containment of the question in the memory, plus a channel prior.

    Containment (not Jaccard) is the right direction here: a long memory that
    contains the question's terms is relevant even though most of its words are
    about something else.
    """

    asked = set(content_tokens(question))
    if not asked:
        return 0.0
    seen = set(content_tokens(content))
    overlap = len(asked & seen) / len(asked)
    channel_prior = 1.0 if LEXICAL_CHANNELS & set(channels) else 0.0
    return min(1.0, _OVERLAP_WEIGHT * overlap + _CHANNEL_WEIGHT * channel_prior)


def _role_of(memory: Any, item: RetrievalItem) -> str:
    """Speaker role, recovered from the structured record or the event type.

    The store keeps provenance but not a role column, so the role is read from
    the structured content when a producer set it and otherwise left as
    ``unknown``. It is never guessed from the wording.
    """

    structured = getattr(memory, "structured_content", None) or {}
    statements = structured.get("_reader_statement_sources") or []
    if statements:
        roles = {source.get("role") or "unknown" for source in statements}
        return next(iter(roles)) if len(roles) == 1 else "mixed"
    positions = structured.get("_reader_sources") or []
    roles = {position["role"] for position in positions}
    if roles:
        return next(iter(roles)) if len(roles) == 1 else "mixed"
    for key in ("role", "source_role", "speaker"):
        value = structured.get(key)
        if value:
            return str(value).casefold()
    if str(structured.get("subject", "")).casefold() == "assistant":
        return "assistant"
    return "unknown"


def build_evidence_views(
    items: Sequence[RetrievalItem],
    *,
    question: str,
    question_date: str | None = None,
) -> list[EvidenceView]:
    """Describe every retrieved item; the order is the retrieval order."""

    cutoff = _instant(question_date)
    groups = conflict_groups(items)
    views: list[EvidenceView] = []
    for index, item in enumerate(items):
        memory = item.memory
        anchor = memory.valid_from or memory.observed_at
        anchor_instant = _instant(anchor)
        days: int | None = None
        if cutoff is not None and anchor_instant is not None:
            days = (cutoff - anchor_instant).days
        views.append(
            EvidenceView(
                index=index,
                memory_id=memory.id,
                content=memory.content,
                source_refs=tuple(item.source_refs),
                channels=tuple(item.channels),
                retrieval_score=float(item.score),
                valid_from=memory.valid_from,
                valid_to=memory.valid_to,
                observed_at=memory.observed_at,
                lifecycle=LifecycleState.of(memory.status),
                role=_role_of(memory, item),
                session_date=memory.valid_from or memory.observed_at,
                days_before_question=days,
                relevance=relevance_proxy(question, memory.content, item.channels),
                # A computed subject/predicate group is the useful name; the raw
                # ``contradicts_id`` is the fallback when no group was detected.
                conflict_group=groups.get(index) or (memory.contradicts_id or None),
                source_positions=tuple(
                    (memory.structured_content or {}).get("_reader_sources", ())
                ),
                statement_sources=tuple(
                    (memory.structured_content or {}).get(
                        "_reader_statement_sources", ()
                    )
                ),
            )
        )
    return views


# --------------------------------------------------------------------------- #
# Conflict detection
# --------------------------------------------------------------------------- #


def _subject_predicate(item: RetrievalItem) -> tuple[str, str] | None:
    structured = getattr(item.memory, "structured_content", None) or {}
    subject = structured.get("subject")
    predicate = structured.get("predicate")
    if subject and predicate:
        return (str(subject).casefold(), str(predicate).casefold())
    return None


#: Predicates whose ``value`` is a whole document rather than a scalar slot.
#:
#: ``statement`` is what ``extraction._fact_from_text`` falls back to for a turn
#: it could not decompose, and ``artifact_document`` is a non-trusted source's
#: deliverable. Two documents are not two competing values for one slot, so
#: including them made *every* pair of formed statements look like a
#: contradiction -- which is precisely how a conflict detector turns into a
#: constant ``CONFLICTING_EVIDENCE`` signal.
DOCUMENT_PREDICATES = frozenset({"statement", "episodic_statement", DOCUMENT_PREDICATE})


def _scalar_value(item: RetrievalItem) -> str | None:
    """The scalar value of a memory, or ``None`` when it has no scalar slot.

    ``None`` means "not a candidate for a contradiction", not "value missing".
    """

    structured = getattr(item.memory, "structured_content", None) or {}
    if str(structured.get("assertion", "")) == DOCUMENT_ASSERTION:
        return None
    predicate = str(structured.get("predicate", "")).casefold()
    if predicate in DOCUMENT_PREDICATES:
        return None
    raw = structured.get("normalized_value")
    if raw is None:
        raw = structured.get("value")
    if raw is None:
        return None
    normalized = str(raw).casefold().strip()
    return normalized or None


def conflict_groups(items: Sequence[RetrievalItem]) -> dict[int, str]:
    """Map item position -> conflict group id, for competing current values.

    Two current memories about the same subject and predicate with different
    *scalar* values are competing. The group id is the canonical key, so a report
    can count groups rather than pairs. Memories whose value is a whole document
    are excluded: they occupy no scalar slot and therefore cannot compete for one.
    """

    buckets: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(items):
        if LifecycleState.of(item.memory.status) is not LifecycleState.CURRENT:
            continue
        key = _subject_predicate(item)
        if key is None:
            continue
        if _scalar_value(item) is None:
            continue
        buckets.setdefault(key, []).append(index)

    groups: dict[int, str] = {}
    for key, positions in buckets.items():
        values = {_scalar_value(items[position]) for position in positions}
        if len(values) < 2:
            continue
        name = f"{key[0]}:{key[1]}"
        for position in positions:
            groups[position] = name
    return groups


def conflicting_groups_among(views: Sequence[EvidenceView]) -> dict[str, list[str]]:
    """Group the views that carry a conflict group id."""

    grouped: dict[str, list[str]] = {}
    for view in views:
        if view.conflict_group:
            grouped.setdefault(view.conflict_group, []).append(view.memory_id)
    return grouped


# --------------------------------------------------------------------------- #
# State classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    state: EvidenceState
    max_relevance: float
    strong_views: int
    weak_views: int
    conflicting: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "max_relevance_proxy": round(self.max_relevance, 4),
            "strong_views": self.strong_views,
            "weak_views": self.weak_views,
            "conflicting_groups": {k: list(v) for k, v in self.conflicting.items()},
            "reason": self.reason,
            "relevance_is_a_proxy": True,
        }


def assess_evidence(
    views: Sequence[EvidenceView],
    *,
    sufficient_threshold: float = SUFFICIENT_RELEVANCE,
    weak_threshold: float = WEAK_RELEVANCE,
) -> EvidenceAssessment:
    """Classify the retrieval set. First matching rule wins."""

    if not views:
        return EvidenceAssessment(
            EvidenceState.NO_EVIDENCE, 0.0, 0, 0, {}, "no evidence was retrieved"
        )
    relevances = [view.relevance for view in views]
    peak = max(relevances)
    strong = sum(1 for value in relevances if value >= sufficient_threshold)
    weak = sum(
        1 for value in relevances if weak_threshold <= value < sufficient_threshold
    )
    groups = conflicting_groups_among(views)

    if strong and groups:
        return EvidenceAssessment(
            EvidenceState.CONFLICTING_EVIDENCE,
            peak,
            strong,
            weak,
            groups,
            "at least two current memories assert different values for the same subject "
            "and predicate, and at least one view is strongly relevant",
        )
    if strong:
        return EvidenceAssessment(
            EvidenceState.SUFFICIENT_EVIDENCE,
            peak,
            strong,
            weak,
            groups,
            "at least one view clears the relevance proxy threshold",
        )
    if weak:
        return EvidenceAssessment(
            EvidenceState.WEAK_EVIDENCE,
            peak,
            strong,
            weak,
            groups,
            "views exist but none clears the relevance proxy threshold",
        )
    return EvidenceAssessment(
        EvidenceState.WEAK_EVIDENCE,
        peak,
        strong,
        weak,
        groups,
        "views exist but none shares a content word with the question",
    )


# --------------------------------------------------------------------------- #
# Question shape + temporal scaffold
# --------------------------------------------------------------------------- #


_DURATION_MARKERS = (
    "how long",
    "how many days",
    "how many weeks",
    "how many months",
    "how many years",
    "how much time",
    "duration",
    "days between",
    "weeks between",
    "months between",
    "before and after",
    "elapsed",
    "in total, how many days",
)
_ORDERING_MARKERS = (
    "before",
    "after",
    "earlier",
    "later",
    "first",
    "last",
    "most recent",
    "latest",
    "which came first",
    "order",
    "precede",
    "follow",
    "previous",
    "next",
)
_COUNT_MARKERS = (
    "how many",
    "how much",
    "count",
    "total number",
    "number of",
    "how often",
)
_TIME_POINT_MARKERS = (
    "when",
    "what date",
    "what day",
    "which day",
    "what year",
    "what month",
)
_PREFERENCE_MARKERS = (
    "like",
    "likes",
    "prefer",
    "preference",
    "favourite",
    "favorite",
    "enjoy",
    "dislike",
    "allergic",
    "avoid",
    "interested in",
)
_ASSISTANT_MARKERS = (
    "you suggested",
    "you recommended",
    "did you recommend",
    "you told me",
    "you mentioned",
    "what did you say",
    "you offered",
    "your advice",
    "you propose",
    "did you suggest",
    "you gave me",
    "what was the name you",
)


def classify_answer_kind(question: str) -> AnswerKind:
    """Classify the answer shape. First matching rule wins."""

    text = str(question).casefold()
    if any(marker in text for marker in _DURATION_MARKERS):
        return AnswerKind.DURATION
    if any(marker in text for marker in _ASSISTANT_MARKERS):
        return AnswerKind.ASSISTANT_RECALL
    if any(marker in text for marker in _COUNT_MARKERS):
        return AnswerKind.COUNT
    if any(marker in text for marker in _ORDERING_MARKERS):
        return AnswerKind.ORDERING
    if any(marker in text for marker in _TIME_POINT_MARKERS):
        return AnswerKind.TIME_POINT
    if any(marker in text for marker in _PREFERENCE_MARKERS):
        return AnswerKind.PREFERENCE
    if any(marker in text for marker in ("list", "which ones", "what are all")):
        return AnswerKind.LIST
    if any(marker in text for marker in ("what", "who", "where", "which")):
        return AnswerKind.FACT
    return AnswerKind.UNKNOWN


#: One reasoning instruction per answer shape. These are the instructions that
#: turn "can I find a sentence that matches?" into "can I compute the answer?".
KIND_INSTRUCTIONS: dict[AnswerKind, str] = {
    AnswerKind.DURATION: (
        "The question asks for an interval. Identify the two dated evidence items it "
        "refers to, then report the interval between their dates as the question asks "
        "(days, weeks or months)."
    ),
    AnswerKind.ORDERING: (
        "The question asks which of several things happened first or last. Order the "
        "dated evidence items and answer with the one the question selects."
    ),
    AnswerKind.COUNT: (
        "The question asks for a number or a sum. Count or add the evidence items that "
        "satisfy the question and answer with that number."
    ),
    AnswerKind.TIME_POINT: (
        "The question asks for a date. Answer with the date carried by the evidence "
        "item the question selects."
    ),
    AnswerKind.PREFERENCE: (
        "The question asks about the user's tastes or habits. Combine every preference "
        "evidence item into one current picture, keeping the most recent polarity for "
        "each thing, and answer from that picture rather than quoting one sentence."
    ),
    AnswerKind.ASSISTANT_RECALL: (
        "The question asks what the assistant previously said, offered or recommended. "
        "Answer from the evidence items marked role=assistant."
    ),
    AnswerKind.LIST: (
        "The question asks for several items. Enumerate the evidence items that satisfy "
        "it and answer with all of them."
    ),
    AnswerKind.FACT: (
        "Answer with the fact the question asks for, taken from the evidence item that "
        "the question's terms select."
    ),
    AnswerKind.UNKNOWN: (
        "Decide what the question asks for from its wording, then answer it from the "
        "evidence rather than from a single matching sentence."
    ),
}


@dataclass(frozen=True, slots=True)
class TemporalScaffold:
    """Dated facts about the evidence, computed rather than inferred."""

    question_date: str | None
    answer_kind: AnswerKind
    earliest_evidence_date: str | None
    latest_evidence_date: str | None
    span_days: int | None
    undated_views: int
    current_views: int
    superseded_views: int
    assistant_views: int
    instruction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_date": self.question_date,
            "answer_kind": self.answer_kind.value,
            "earliest_evidence_date": self.earliest_evidence_date,
            "latest_evidence_date": self.latest_evidence_date,
            "span_days": self.span_days,
            "undated_views": self.undated_views,
            "current_views": self.current_views,
            "superseded_views": self.superseded_views,
            "assistant_views": self.assistant_views,
            "instruction": self.instruction,
        }


def build_temporal_scaffold(
    question: str,
    question_date: str | None,
    views: Sequence[EvidenceView],
) -> TemporalScaffold:
    """Summarise the dated structure of the evidence for the prompt."""

    dated = [view for view in views if _instant(view.session_date) is not None]
    ordered = sorted(dated, key=lambda view: _instant(view.session_date))  # type: ignore[arg-type,return-value]
    earliest = ordered[0].session_date if ordered else None
    latest = ordered[-1].session_date if ordered else None
    span: int | None = None
    if len(ordered) >= 2:
        first, last = (
            _instant(ordered[0].session_date),
            _instant(ordered[-1].session_date),
        )
        if first is not None and last is not None:
            span = (last - first).days
    kind = classify_answer_kind(question)
    return TemporalScaffold(
        question_date=question_date,
        answer_kind=kind,
        earliest_evidence_date=earliest,
        latest_evidence_date=latest,
        span_days=span,
        undated_views=len(views) - len(dated),
        current_views=sum(1 for view in views if view.is_current),
        superseded_views=sum(
            1 for view in views if view.lifecycle is LifecycleState.SUPERSEDED
        ),
        assistant_views=sum(1 for view in views if view.role == "assistant"),
        instruction=KIND_INSTRUCTIONS[kind],
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_evidence_block(views: Sequence[EvidenceView]) -> str:
    """The annotated evidence block that replaces the flat v1 context.

    One line per memory, front-loaded with the fields the reader has to reason
    over. The memory content stays verbatim so a citation quote remains a
    substring of what the reader saw.
    """

    lines: list[str] = []
    if any(view.statement_sources for view in views):
        lines.append(
            "Statement provenance dates the messages, not necessarily the events. "
            "Entries use the same 1-based order as source=. Resolve relative phrases "
            "against their own statement_at; explicit event dates and plan/completion "
            "qualifiers still apply. Multiple source times remain distinct; unknown "
            "times do not establish an event date."
        )
    for view in views:
        valid = f"{view.valid_from or '?'}..{view.valid_to or 'present'}"
        bits = [
            f"id={view.memory_id}",
            f"source={','.join(view.source_refs) or 'unknown'}",
            f"valid={valid}",
            f"state={view.lifecycle.value}",
            f"role={view.role}",
        ]
        if view.days_before_question is not None:
            bits.append(f"days_before_question={view.days_before_question}")
        if view.conflict_group:
            bits.append(f"conflict_group={view.conflict_group}")
        if view.source_positions:
            bits.append(
                "source_order="
                + ",".join(
                    f"s{p['session_index']}:t{p['turn_index']}:{p['role']}"
                    for p in view.source_positions
                )
            )
        if view.statement_sources:
            sources = {
                source["source_ref"]: source for source in view.statement_sources
            }
            bits.append(
                "statement_provenance="
                + ",".join(
                    f"{i}:{sources.get(ref, {}).get('role') or 'unknown'}"
                    f"@statement_at={sources.get(ref, {}).get('statement_at') or '?'}"
                    for i, ref in enumerate(view.source_refs, 1)
                )
            )
        lines.append(f"[{view.index}] {'; '.join(bits)}\n    {view.content}")
    return "\n".join(lines)


def summarize_views(views: Sequence[EvidenceView]) -> dict[str, Any]:
    """A compact, JSON-safe description for the trace."""

    return {
        "count": len(views),
        "views": [view.to_dict() for view in views],
    }
