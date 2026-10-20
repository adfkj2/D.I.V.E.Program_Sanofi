"""Backward-compatible exports; implementation lives in dive_memory.reading.

Existing Oracle imports and historical policy identifiers remain supported.
"""

from ..reading.premise_review_reader import (
    REVIEW_VERSION as REVIEW_VERSION,
)
from ..reading.premise_review_reader import (
    RULES as RULES,
)
from ..reading.premise_review_reader import (
    VERSION as VERSION,
)
from ..reading.premise_review_reader import (
    IndexedEvidenceReader as IndexedEvidenceReader,
)
from ..reading.premise_review_reader import (
    NecessaryPremiseReader as NecessaryPremiseReader,
)
from ..reading.premise_review_reader import (
    ReaderSpec as ReaderSpec,
)
from ..reading.premise_review_reader import (
    ReaderStatus as ReaderStatus,
)
from ..reading.premise_review_reader import (
    RefusalReviewReader as RefusalReviewReader,
)
from ..reading.premise_review_reader import (
    combine as combine,
)
