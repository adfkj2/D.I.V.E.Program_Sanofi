"""Opt-in final refusal review using only the unchanged reader request."""

import json
from hashlib import sha256

from .adjudicated_reader import combine
from .contracts import ReaderStatus
from .indexed_reader import RULES, IndexedEvidenceReader
from .reader import ReaderSpec

REVIEW_VERSION = "oracle-necessary-premise-review-v1"
VERSION = "oracle-refusal-premise-review-v1"


class NecessaryPremiseReader(IndexedEvidenceReader):
    prompt_version = REVIEW_VERSION
    rules = (
        RULES
        + """
Perform a necessary-premise audit before choosing ANSWER or ABSTAIN:
1. Identify the fact actually requested and the minimum inputs needed to determine it.
   Locate those inputs across the supplied passages. Keep distinct entities and events separate.
2. Distinguish a missing input from a missing derived result. If supported inputs determine a
   sum, difference, duration, order, adjusted time or count, carry out that operation. Ordinary
   arithmetic and calendar rules do not need to be stated in the conversation. Do not require
   a separate explicit total, elapsed duration or sentence stating the final answer.
3. Anchor relative time to the date of the statement containing it; distinguish event time
   from record time. Compare supported dates or intervals. Do not assume an undated past event
   happened on its record date, or impose the question date when another event is the reference.
   Preserve approximate quantities; do not turn an estimate into unjustified precision.
4. For counts, enumerate the distinct qualifying items actually described, merging duplicate
   mentions. For updates, distinguish a cumulative total from a new increment. Do not invent
   missing items or add incompatible scopes. A complete list need not include a stated count.
5. A supported description can identify an unnamed item when the question does not require a
   proper name. A requested recommendation can use known needs and preferences without proof
   that the recommendation already happened. Do not invent personal facts or live local events.
6. Before ABSTAIN, check that each proposed missing premise is truly absent and necessary.
   If it is present, use it; if a required entity, relationship, amount or time is absent,
   abstain. Do not use the wording of the question as proof of an unrecorded relationship.
Keep this audit internal. Return exactly the JSON schema above and cite the actual premises.
"""
    )


class RefusalReviewReader:
    """One review after a genuine refusal; errors and existing answers pass through."""

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
        if first.status != ReaderStatus.ABSTAIN:
            return combine(first, [first], "unchanged_non_refusal", version=VERSION)
        # No prior answer/refusal, evaluator label or hidden history enters review.
        reviewed = self.reviewer.generate(request)
        return combine(
            reviewed, [first, reviewed], "necessary_premise_review", version=VERSION
        )
