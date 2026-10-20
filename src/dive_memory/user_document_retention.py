"""Opt-in retention of explicitly requested user material as source documents."""
import re
from dataclasses import dataclass

from .extraction import _document_candidate
from .gate import GateAction, GateDecision, _sensitive

VERSION = 'explicit-user-document-v1'
# Match a direct request at a sentence boundary near the start, not an embedded
# quotation, a recollection ("I remember"), or a negated request.
_REQUEST = re.compile(
    r'(?:^|[.!?。！？]\s+|\n)\s*(?:'
    r'(?:(?:please\s+)?(?:remember|save|store|keep in mind)\s+(?:this|these|the following)\b)'
    r'|(?:I\s+(?:want|need|would like)\s+you\s+to\s+(?:remember|save|store)\s+(?:this|these|the following)\b)'
    r'|(?:(?:请)?(?:记住|保存|记录)(?:这|以下))'
    r')', re.I)


@dataclass(frozen=True)
class ExplicitUserDocumentRetention:
    base: object | None = None
    min_chars: int = 200
    max_chars: int = 8000

    def __post_init__(self):
        if not 0 < self.min_chars <= self.max_chars:
            raise ValueError('invalid user document length bounds')

    def candidates(self, text, *, source_type, observed_at, existing):
        prior = self.base.candidates(text, source_type=source_type,
            observed_at=observed_at, existing=existing) if self.base else []
        stripped = text.strip()
        if (source_type != 'user' or not self.min_chars <= len(stripped) <= self.max_chars
                or _sensitive(text) or not _REQUEST.search(stripped[:400])):
            return prior
        sentences = [s for s in re.split(r'[.!?。！？](?:\s+|$)|\n+', stripped) if s.strip()]
        if len(sentences) < 3:
            return prior
        if any(c.content.strip() == stripped for c in [*existing, *prior]):
            return prior
        decision = GateDecision(True, .6, .8, .65, 'long_term',
            'user explicitly requested retention of this source document', GateAction.WRITE,
            'EXPLICIT_USER_DOCUMENT', {'document_chars':float(len(stripped)),
            'explicit_remember_request':1.0}, VERSION)
        candidate = _document_candidate(stripped, decision, source_type, observed_at)
        start = len(text) - len(text.lstrip())
        candidate.structured_content.update(source_span=[start, start+len(stripped)],
            retention_policy=VERSION, context_may_include_questions_or_hypotheses=True)
        return [*prior, candidate]
