"""Backward-compatible exports; implementation lives in dive_memory.reading.

Existing Oracle imports and historical policy identifiers remain supported.
"""

from ..reading.evidence import (
    _ASSISTANT_MARKERS as _ASSISTANT_MARKERS,
)
from ..reading.evidence import (
    _CHANNEL_WEIGHT as _CHANNEL_WEIGHT,
)
from ..reading.evidence import (
    _COUNT_MARKERS as _COUNT_MARKERS,
)
from ..reading.evidence import (
    _DURATION_MARKERS as _DURATION_MARKERS,
)
from ..reading.evidence import (
    _ORDERING_MARKERS as _ORDERING_MARKERS,
)
from ..reading.evidence import (
    _OVERLAP_WEIGHT as _OVERLAP_WEIGHT,
)
from ..reading.evidence import (
    _PREFERENCE_MARKERS as _PREFERENCE_MARKERS,
)
from ..reading.evidence import (
    _STOPWORDS as _STOPWORDS,
)
from ..reading.evidence import (
    _TIME_POINT_MARKERS as _TIME_POINT_MARKERS,
)
from ..reading.evidence import (
    _TOKEN as _TOKEN,
)
from ..reading.evidence import (
    DOCUMENT_ASSERTION as DOCUMENT_ASSERTION,
)
from ..reading.evidence import (
    DOCUMENT_PREDICATE as DOCUMENT_PREDICATE,
)
from ..reading.evidence import (
    DOCUMENT_PREDICATES as DOCUMENT_PREDICATES,
)
from ..reading.evidence import (
    KIND_INSTRUCTIONS as KIND_INSTRUCTIONS,
)
from ..reading.evidence import (
    LEXICAL_CHANNELS as LEXICAL_CHANNELS,
)
from ..reading.evidence import (
    SUFFICIENT_RELEVANCE as SUFFICIENT_RELEVANCE,
)
from ..reading.evidence import (
    WEAK_RELEVANCE as WEAK_RELEVANCE,
)
from ..reading.evidence import (
    AnswerKind as AnswerKind,
)
from ..reading.evidence import (
    EvidenceAssessment as EvidenceAssessment,
)
from ..reading.evidence import (
    EvidenceState as EvidenceState,
)
from ..reading.evidence import (
    EvidenceView as EvidenceView,
)
from ..reading.evidence import (
    LifecycleState as LifecycleState,
)
from ..reading.evidence import (
    MemoryStatus as MemoryStatus,
)
from ..reading.evidence import (
    RetrievalItem as RetrievalItem,
)
from ..reading.evidence import (
    TemporalScaffold as TemporalScaffold,
)
from ..reading.evidence import (
    _instant as _instant,
)
from ..reading.evidence import (
    _role_of as _role_of,
)
from ..reading.evidence import (
    _scalar_value as _scalar_value,
)
from ..reading.evidence import (
    _subject_predicate as _subject_predicate,
)
from ..reading.evidence import (
    assess_evidence as assess_evidence,
)
from ..reading.evidence import (
    build_evidence_views as build_evidence_views,
)
from ..reading.evidence import (
    build_temporal_scaffold as build_temporal_scaffold,
)
from ..reading.evidence import (
    classify_answer_kind as classify_answer_kind,
)
from ..reading.evidence import (
    conflict_groups as conflict_groups,
)
from ..reading.evidence import (
    conflicting_groups_among as conflicting_groups_among,
)
from ..reading.evidence import (
    content_tokens as content_tokens,
)
from ..reading.evidence import (
    relevance_proxy as relevance_proxy,
)
from ..reading.evidence import (
    render_evidence_block as render_evidence_block,
)
from ..reading.evidence import (
    summarize_views as summarize_views,
)
