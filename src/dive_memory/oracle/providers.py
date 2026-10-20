"""Backward-compatible exports; implementation lives in dive_memory.reading.

Existing Oracle imports and historical policy identifiers remain supported.
"""

from ..reading.providers import (
    ChatCompletion as ChatCompletion,
)
from ..reading.providers import (
    ChatTransportError as ChatTransportError,
)
from ..reading.providers import (
    OpenAICompatibleChatClient as OpenAICompatibleChatClient,
)
from ..reading.providers import (
    ScriptedChatClient as ScriptedChatClient,
)
from ..reading.providers import (
    _as_int as _as_int,
)
