"""Sequential reader: an initial refusal cannot be overridden by a second model.

Routing uses only the first grounded outcome. Both readers receive the same
budget-visible request; neither receives reference answers or the other's answer.
"""
from dataclasses import replace
from hashlib import sha256
import json

from .contracts import ReaderStatus
from .reader import ReaderSpec, TokenUsage

VERSION = "oracle-refusal-gated-thinking-v1"


class RefusalGatedReader:
    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary
        self.policy = primary.policy
        children = {"primary": primary.spec.to_dict(), "secondary": secondary.spec.to_dict()}
        self.spec = ReaderSpec(primary.spec.provider, primary.spec.model,
            revision=sha256(json.dumps(children, sort_keys=True).encode()).hexdigest(),
            prompt_version=VERSION, max_tokens=secondary.spec.max_tokens)

    def generate(self, request):
        first = self.primary.generate(request)
        if first.status != ReaderStatus.ANSWER:
            # Preserve genuine refusal AND invalid/transport failures as such.
            return replace(first, policy_version=VERSION,
                detail=json.dumps({"route": "primary_stop", "primary": first.to_dict()}, ensure_ascii=False))
        second = self.secondary.generate(request)

        def total(field):
            values = [getattr(outcome.usage, field) for outcome in (first, second)]
            return sum(values) if all(type(value) is int for value in values) else None

        inputs, outputs = total("input_tokens"), total("output_tokens")
        known = inputs is not None and outputs is not None
        return replace(second, policy_version=VERSION,
            usage=TokenUsage(inputs, outputs,
                "provider" if known and first.usage.source == second.usage.source == "provider" else "mixed_or_unknown"),
            calls=first.calls + second.calls, latency_ms=first.latency_ms + second.latency_ms,
            prompt_sha256=sha256(json.dumps([first.prompt_sha256, second.prompt_sha256]).encode()).hexdigest(),
            response_sha256=sha256(json.dumps([first.response_sha256, second.response_sha256]).encode()).hexdigest(),
            attempt_errors=first.attempt_errors + second.attempt_errors,
            detail=json.dumps({"route": "secondary_after_primary_answer",
                "primary": first.to_dict(), "secondary": second.to_dict()}, ensure_ascii=False))
