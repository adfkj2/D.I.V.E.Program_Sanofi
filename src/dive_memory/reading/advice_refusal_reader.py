"""Review genuine refusals to current advice requests using visible evidence only."""

import json
import re
from hashlib import sha256

from .adjudicated_reader import combine
from .contracts import ReaderStatus
from .reader import ReaderSpec

VERSION = "oracle-advice-refusal-review-v1"
_REQUEST = re.compile(
    r"\b(?:can|could|would) you (?:suggest|recommend|give\b[^?.!]*\b(?:advice|tips))\b"
    r"|\bany (?:tips|advice|suggestions)\b|\b(?:what|do) you think\b"
    r"|\b(?:what|which|how) should I\b|\bshould I\b",
    re.I,
)
_RECALL = re.compile(
    r"\b(?:remind|remember|recall|previous conversation|previous chat|last conversation)\b"
    r"|\b(?:you|we) (?:previously |earlier )?(?:recommended|suggested|discussed|decided)\b"
    r"|\bshould I have\b",
    re.I,
)
_ZH_REQUEST = re.compile(
    r"有(?:什么|哪些).*建议|你(?:能|可以).*推荐|我(?:应该|该).*|你觉得.*(?:如何|怎么样|好吗)"
)
_ZH_RECALL = re.compile(r"之前|上次|记得|回忆")


def advice_question(question):
    if not isinstance(question, str):
        raise TypeError("question must be text")
    if _RECALL.search(question) or _ZH_RECALL.search(question):
        return False
    return bool(_REQUEST.search(question) or _ZH_REQUEST.search(question))


class AdviceRefusalReader:
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
        if first.status != ReaderStatus.ABSTAIN or not advice_question(
            request.question
        ):
            return combine(
                first, [first], "unchanged_non_advice_refusal", version=VERSION
            )
        reviewed = self.reviewer.generate(request)
        return combine(
            reviewed, [first, reviewed], "advice_premise_review", version=VERSION
        )
