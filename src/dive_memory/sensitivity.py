from __future__ import annotations

from .gate import SENSITIVE_PATTERNS


def sensitivity_labels(text: str) -> tuple[str, ...]:
    return ("secret",) if any(pattern.search(text) for pattern in SENSITIVE_PATTERNS) else ()
