from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .gate import GateDecision, decide

# A gate is any callable matching ``decide``'s keyword-only signature. The
# default stays the v1 keyword policy; v2 (``semantic_gate.SemanticGate``) can
# be injected without touching this module or its callers.
GatePolicy = Callable[..., GateDecision]


@dataclass(slots=True)
class Candidate:
    content: str
    kind: str
    evidence_state: str
    structured_content: dict[str, Any]
    decision: GateDecision
    valid_from: str | None = None
    valid_to: str | None = None


def _fact_from_text(text: str) -> tuple[str, dict[str, Any], str]:
    patterns = [
        (r"我住在\s*([^。,.，]+)", "residence", "semantic_fact"),
        (r"我(?:现在)?主要使用\s*([^。,.，]+)", "primary_tool", "semantic_fact"),
        (r"我喜欢\s*([^。,.，]+)", "preference", "preference"),
        (r"我偏好\s*([^。,.，]+)", "preference", "preference"),
        (r"我的目标是\s*([^。,.，]+)", "goal", "semantic_fact"),
    ]
    for pattern, predicate, kind in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1).strip()
            return f"{predicate}: {value}", _subject_record(SUBJECT_USER, predicate, value), kind
    # --- third-party statements -------------------------------------------
    # "Alice lives in Shanghai" must be *remembered* (it is a fact the user
    # chose to disclose) while never being projected onto the user's own
    # profile. The distinction is the subject, not the predicate: the memory
    # carries ``subject='alice'`` so the residency rules for the user do not
    # apply to her. This is the boundary the gate must not be asked to police —
    # provenance and entity ownership are different questions.
    third_party = _third_party_fact(text)
    if third_party is not None:
        subject, predicate, value, kind = third_party
        return f"{subject} {predicate}: {value}", _subject_record(subject, predicate, value), kind
    lowered = text.lower()
    if re.search(r"https?://", text):
        kind = "resource"
    elif _explicit_date(text):
        kind = "temporal_event"
    elif any(marker in lowered for marker in ("上次", "曾经", "last time", "previously")):
        kind = "episode"
    elif any(marker in lowered for marker in ("步骤", "流程", "怎么做", "procedure", "workflow")):
        kind = "procedural"
    else:
        kind = "semantic_fact"
    return text.strip(), _subject_record(SUBJECT_USER, "statement", text.strip()), kind


def _subject_record(subject: str, predicate: str, value: str) -> dict[str, Any]:
    return {"subject": subject, "predicate": predicate, "value": value}


# ---------------------------------------------------------------------------
# Third-party facts
# ---------------------------------------------------------------------------
SUBJECT_USER = "user"

# Predicates that describe *where a person lives*. Only the user's own
# residence belongs in the profile; someone else's must not be projected there.
_PERSON_PREDICATES = {
    "lives in": "residence",
    "lives at": "residence",
    "resides in": "residence",
    "works at": "employer",
    "works for": "employer",
    "is married to": "spouse",
}

_THIRD_PARTY_PATTERNS = tuple(
    (re.compile(rf"^(?P<subject>[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)?)\s+{verb}\s+(?P<value>[^.;]+)", re.I),
     predicate)
    for verb, predicate in _PERSON_PREDICATES.items()
)

# First-person and common pronouns are never "third parties".
_NON_THIRD_PARTY_SUBJECTS = frozenset({
    "i", "we", "you", "he", "she", "they", "it", "this", "that", "there",
})


def _third_party_fact(text: str) -> tuple[str, str, str, str] | None:
    """Detect ``<Named subject> <person predicate> <value>`` statements.

    Returns ``(subject, predicate, value, kind)`` with the subject lowercased
    so the same person mentioned twice resolves to one key, or ``None`` when the
    text is not a third-party personal fact.
    """
    for pattern, predicate in _THIRD_PARTY_PATTERNS:
        match = pattern.match(text.strip())
        if not match:
            continue
        subject = match.group("subject").strip()
        if subject.casefold() in _NON_THIRD_PARTY_SUBJECTS:
            continue
        value = match.group("value").strip()
        if not value:
            continue
        return subject.casefold(), predicate, value, "semantic_fact"
    return None


def _explicit_date(text: str) -> str | None:
    match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?", text)
    if not match:
        return None
    year, month, day = (int(value) for value in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}T00:00:00+00:00"


def extract_candidates(text: str, *, explicit: bool = False, observed_at: str | None = None,
                       gate: GatePolicy | None = None,
                       source_type: str | None = None) -> list[Candidate]:
    policy = gate or decide
    # ``source_type`` is only forwarded to policies that accept it; the v1
    # ``decide`` has no such parameter and must keep working unchanged.
    if _accepts_source_type(policy):
        decision = policy(text, explicit=explicit, source_type=source_type)
    else:
        decision = policy(text, explicit=explicit)
    if not decision.accepted:
        return []
    content, structured, kind = _fact_from_text(text)
    return [Candidate(content, kind, "FACT", structured, decision, _explicit_date(text) or observed_at)]


def _accepts_source_type(policy: GatePolicy) -> bool:
    """True when the policy advertises support for a ``source_type`` keyword."""
    return bool(getattr(policy, "supports_source_type", False))
