"""Independent evidence review only when two grounded readers disagree on answerability."""

import json
from dataclasses import replace
from hashlib import sha256

from .contracts import ReaderStatus
from .indexed_reader import RULES, IndexedEvidenceReader
from .reader import ReaderSpec, TokenUsage

VERSION = "oracle-adjudicated-reader-v1"
FORMAT_RECOVERY_VERSION = "oracle-adjudicated-format-recovery-v1"
REFEREE_VERSION = "oracle-answerability-referee-v1"


class EvidenceReferee(IndexedEvidenceReader):
    prompt_version = REFEREE_VERSION
    rules = (
        RULES
        + """
Two readers disagreed about whether this question can be answered. Independently review ONLY
the supplied evidence. Neither a prior answer nor a prior refusal is proof.
Check the requested person, object, event, attribute and time separately. Facts about a related
course, project, relative or device do not establish the requested relationship. A number with
the wrong scope is not an answer. A current cumulative value is not a change between updates.
For history questions require the necessary factual premises, not just a topical overlap.
For suggestions, supported preferences can justify a suitable recommendation; the suggestion
need not have happened before. Do not demand an irrelevant premise or an explicit answer sentence.
Use ANSWER only if each necessary factual premise is supported; otherwise use ABSTAIN with the
specific missing premise. Quotes must remain exact and indices must identify visible evidence.
"""
    )


def recoverable_secondary_format(primary_status, secondary_status):
    """A malformed second response need not discard a validated first outcome.

    This never replaces a genuine refusal, a transport error, or a timeout.
    Routing depends only on runtime statuses, never correctness or case identity.
    """
    return secondary_status == ReaderStatus.INVALID and primary_status in {
        ReaderStatus.ANSWER,
        ReaderStatus.ABSTAIN,
    }


def combine(chosen, stages, route, *, version=VERSION):
    def total(field):
        values = [getattr(stage.usage, field) for stage in stages]
        return sum(values) if all(type(value) is int for value in values) else None

    inputs, outputs = total("input_tokens"), total("output_tokens")
    known = inputs is not None and outputs is not None
    return replace(
        chosen,
        policy_version=version,
        usage=TokenUsage(
            inputs,
            outputs,
            "provider"
            if known and all(stage.usage.source == "provider" for stage in stages)
            else "mixed_or_unknown",
        ),
        calls=sum(stage.calls for stage in stages),
        latency_ms=sum(stage.latency_ms for stage in stages),
        prompt_sha256=sha256(
            json.dumps([stage.prompt_sha256 for stage in stages]).encode()
        ).hexdigest(),
        response_sha256=sha256(
            json.dumps([stage.response_sha256 for stage in stages]).encode()
        ).hexdigest(),
        attempt_errors=tuple(
            error for stage in stages for error in stage.attempt_errors
        ),
        detail=json.dumps(
            {"route": route, "stages": [stage.to_dict() for stage in stages]},
            ensure_ascii=False,
        ),
    )


class AdjudicatedReader:
    rules: str

    def __init__(self, primary, secondary, referee, *, recover_invalid_secondary=False):
        if type(recover_invalid_secondary) is not bool:
            raise ValueError("recover_invalid_secondary must be boolean")
        self.primary, self.secondary, self.referee = primary, secondary, referee
        self.policy = primary.policy
        self.recover_invalid_secondary = recover_invalid_secondary
        self.version = FORMAT_RECOVERY_VERSION if recover_invalid_secondary else VERSION
        digest = sha256(
            json.dumps(
                [r.spec.to_dict() for r in (primary, secondary, referee)],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        # Configuration identity is not a claimed provider model revision.
        self.spec = ReaderSpec(
            primary.spec.provider,
            primary.spec.model,
            prompt_version=self.version + "/" + digest[:16],
            max_tokens=secondary.spec.max_tokens,
        )

    def _combine(self, chosen, stages, route):
        return combine(chosen, stages, route, version=self.version)

    def generate(self, request):
        first = self.primary.generate(request)
        if first.status in {ReaderStatus.ERROR, ReaderStatus.TIMEOUT}:
            return self._combine(first, [first], "primary_transport_failure")
        second = self.secondary.generate(request)
        stages = [first, second]
        if second.is_failure:
            if self.recover_invalid_secondary and recoverable_secondary_format(
                first.status, second.status
            ):
                return self._combine(first, stages, "primary_after_secondary_invalid")
            return self._combine(second, stages, "secondary_failure")
        if first.status == ReaderStatus.INVALID:
            return self._combine(second, stages, "recovered_primary_format")
        if first.status == second.status:
            return self._combine(second, stages, "same_answerability")
        # No first/second answer or benchmark metadata is injected into review.
        reviewed = self.referee.generate(request)
        return self._combine(
            reviewed, [*stages, reviewed], "independent_evidence_review"
        )
