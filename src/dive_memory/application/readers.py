"""Compose readers without experiment scripts, result files or gold labels.

The r40 strategy preserves the historical prompt/parameter composition. It is
an explicitly selected development strategy, not a claim of official accuracy.
"""

from typing import Protocol

from ..oracle.adjudicated_reader import (
    REFEREE_VERSION,
    AdjudicatedReader,
    EvidenceReferee,
)
from ..oracle.advice_refusal_reader import AdviceRefusalReader
from ..oracle.current_state_reader import REVIEW_VERSION as STATE_VERSION
from ..oracle.current_state_reader import CurrentStateReader, CurrentStateReviewReader
from ..oracle.grounded_advice_reader import VERSION as ADVICE_VERSION
from ..oracle.grounded_advice_reader import GroundedAdviceReader
from ..oracle.indexed_reader import IndexedEvidenceReader
from ..oracle.premise_review_reader import REVIEW_VERSION, NecessaryPremiseReader
from ..oracle.reader import (
    ChatClient,
    OracleReaderRequest,
    ReaderOutcome,
    ReaderPolicy,
    ReaderSpec,
)
from ..oracle.temporal_refusal_reader import TemporalRefusalReader


class Reader(Protocol):
    spec: ReaderSpec
    policy: ReaderPolicy

    def generate(self, request: OracleReaderRequest) -> ReaderOutcome: ...


def create_reader(
    normal: ChatClient,
    thinking: ChatClient | None = None,
    *,
    model: str,
    provider: str = "openai-compatible",
    strategy: str = "indexed",
) -> Reader:
    if strategy not in {"indexed", "r40"}:
        raise ValueError("strategy must be indexed or r40")
    policy = ReaderPolicy(
        name="indexed_evidence",
        prompt_version="oracle-indexed-reader-v2",
        max_refusal_retries=0,
    )
    primary = IndexedEvidenceReader(
        normal,
        ReaderSpec(
            provider, model, prompt_version=policy.prompt_version, max_tokens=512
        ),
        policy=policy,
    )
    if strategy == "indexed":
        return primary
    if thinking is None:
        raise ValueError("r40 requires an explicit thinking-mode transport")
    secondary = IndexedEvidenceReader(
        thinking,
        ReaderSpec(
            provider,
            model,
            prompt_version=policy.prompt_version,
            max_tokens=8192,
            timeout_seconds=120,
        ),
        policy=policy,
    )
    referee = EvidenceReferee(
        normal,
        ReaderSpec(provider, model, prompt_version=REFEREE_VERSION, max_tokens=1024),
        policy=policy,
    )
    reader = AdjudicatedReader(
        primary, secondary, referee, recover_invalid_secondary=True
    )
    reader.rules = secondary.rules

    def premise():
        return NecessaryPremiseReader(
            thinking,
            ReaderSpec(
                provider,
                model,
                prompt_version=REVIEW_VERSION,
                max_tokens=8192,
                timeout_seconds=120,
            ),
            policy=policy,
        )

    temporal = TemporalRefusalReader(reader, premise())
    advice = AdviceRefusalReader(temporal, premise())
    grounded = GroundedAdviceReader(
        thinking,
        ReaderSpec(
            provider,
            model,
            prompt_version=ADVICE_VERSION,
            max_tokens=8192,
            timeout_seconds=120,
        ),
        policy=policy,
    )
    advised = AdviceRefusalReader(advice, grounded)
    state = CurrentStateReader(
        thinking,
        ReaderSpec(
            provider,
            model,
            prompt_version=STATE_VERSION,
            max_tokens=8192,
            timeout_seconds=120,
        ),
        policy=policy,
    )
    return CurrentStateReviewReader(advised, state)
