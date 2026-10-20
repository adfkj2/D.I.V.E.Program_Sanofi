"""Independent state review after a valid answer with multiple visible source dates."""

import json
import re
from datetime import datetime
from hashlib import sha256

from .adjudicated_reader import combine
from .advice_refusal_reader import advice_question
from .contracts import ReaderStatus
from .indexed_reader import RULES, IndexedEvidenceReader
from .reader import ReaderSpec

REVIEW_VERSION = "oracle-current-state-premises-v1"
VERSION = "oracle-current-state-review-v1"
_EXCLUDE = re.compile(
    r"\b(?:previous|previously|initial|initially|before|earliest)\b"
    r"|\b(?:order of|chronological order|chronology)\b"
    r"|^\s*how (?:many|long)\b|\btotal number\b",
    re.I,
)
_STATE = re.compile(
    r"\b(?:current(?:ly)?|now|latest|recent|usually|personal best|highest score)\b",
    re.I,
)
_HABIT = re.compile(r"^\s*what time do I\b|^\s*where\b.*\b(?:keep|live|work)\b", re.I)


def current_state_question(question):
    if not isinstance(question, str):
        raise TypeError("question must be text")
    return (
        not advice_question(question)
        and not _EXCLUDE.search(question)
        and bool(_STATE.search(question) or _HABIT.search(question))
    )


def multiple_source_dates(request):
    dates = set()
    for item in request.items:
        # The existing evidence header exposes valid_from. Do not route using
        # a hidden observed_at fallback when the visible date is unknown.
        value = item.memory.valid_from
        if not value:
            continue
        try:
            dates.add(datetime.fromisoformat(str(value).replace("Z", "+00:00")).date())
        except (ValueError, TypeError):
            continue
    return len(dates) >= 2


class CurrentStateReader(IndexedEvidenceReader):
    prompt_version = REVIEW_VERSION
    rules = (
        RULES
        + """
Audit the requested state or habitual attribute across the visible dated statements:
1. Identify the exact subject, attribute and reference period. Compare all relevant statements
   about that same attribute; retrieval rank and repeated copies are not chronological priority.
   A later report of the same ongoing attribute can update an older one. Different people,
   objects, episodes or explicitly historical periods must remain distinct.
2. Determine the scope of modality at the clause level. A question, goal or planned action can
   contain an asserted existing background attribute in a possessive, relative or causal clause.
   The future action does not make its already-established background property hypothetical.
   Conversely, a desired new value, an intention to move, or a planned achievement does not by
   itself establish that the new state happened. Preserve this distinction rather than rejecting
   or accepting a whole sentence because it contains a planning word.
3. For a usual routine, consider the latest supported habitual description, including the user's
   stated habitual preference. Do not demand a separate log proving each occurrence. Distinguish
   an existing routine from an expressly proposed future change.
4. Memory state=CURRENT is storage metadata, not proof that every conflicting value is still true.
   Use dates as statement anchors, while respecting explicit event dates and historical qualifiers.
   Do not invent intervening events to reconcile incompatible statements. Use the most recent
   applicable established value; if a necessary distinction remains unresolved, abstain.
Keep the reasoning internal. Return the original JSON schema. Cite exact body text establishing
the chosen attribute; metadata headers can guide dates but must never be copied as body quotes.
"""
    )


class CurrentStateReviewReader:
    def __init__(self, primary, reviewer):
        self.primary, self.reviewer = primary, reviewer
        self.policy = primary.policy
        digest = sha256(
            json.dumps(
                [r.spec.to_dict() for r in (primary, reviewer)], sort_keys=True
            ).encode()
        ).hexdigest()
        self.spec = ReaderSpec(
            primary.spec.provider,
            primary.spec.model,
            prompt_version=VERSION + "/" + digest[:16],
            max_tokens=reviewer.spec.max_tokens,
        )

    def generate(self, request):
        first = self.primary.generate(request)
        if (
            first.status != ReaderStatus.ANSWER
            or not current_state_question(request.question)
            or not multiple_source_dates(request)
        ):
            return combine(
                first, [first], "unchanged_non_current_state", version=VERSION
            )
        reviewed = self.reviewer.generate(request)
        return combine(
            reviewed, [first, reviewed], "current_state_review", version=VERSION
        )
