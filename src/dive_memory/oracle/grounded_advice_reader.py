"""Backward-compatible exports; implementation lives in dive_memory.reading.

Existing Oracle imports and historical policy identifiers remain supported.
"""

from ..reading.grounded_advice_reader import (
    RULES as RULES,
)
from ..reading.grounded_advice_reader import (
    VERSION as VERSION,
)
from ..reading.grounded_advice_reader import (
    GroundedAdviceReader as GroundedAdviceReader,
)
from ..reading.grounded_advice_reader import (
    IndexedEvidenceReader as IndexedEvidenceReader,
)
