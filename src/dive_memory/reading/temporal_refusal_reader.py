"""Question-text temporal routing after a valid refusal; no evaluator metadata."""

import json
import re
from hashlib import sha256

from .adjudicated_reader import combine
from .contracts import ReaderStatus
from .reader import ReaderSpec

VERSION = "oracle-temporal-refusal-review-v1"
_DURATION = r"(?:days?|weeks?|months?|years?|hours?|minutes?|seconds?)"
_COUNT = re.compile(r"^\s*how many\s+(?!" + _DURATION + r"\b)", re.I)
_DIRECT = re.compile(
    r"^\s*(?:when\b|what time\b|how long\b|how many\s+" + _DURATION + r"\b|how old\b)",
    re.I,
)
_ORDER = re.compile(
    r"^\s*(?:who|which|what)\b.*\b(?:first|earliest|latest|before|after|last|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_ZH = re.compile(
    r"何时|什么时候|几点|多久|多少(?:天|周|个月|年|小时|分钟)|谁先|哪个.*(?:先|后)|上周[一二三四五六日天]"
)


def temporal_question(question):
    if not isinstance(question, str):
        raise TypeError("question must be text")
    if _COUNT.search(question):
        return False
    return bool(
        _DIRECT.search(question) or _ORDER.search(question) or _ZH.search(question)
    )


class TemporalRefusalReader:
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
        if first.status != ReaderStatus.ABSTAIN or not temporal_question(
            request.question
        ):
            return combine(
                first, [first], "unchanged_non_temporal_refusal", version=VERSION
            )
        reviewed = self.reviewer.generate(request)
        return combine(
            reviewed, [first, reviewed], "temporal_premise_review", version=VERSION
        )
