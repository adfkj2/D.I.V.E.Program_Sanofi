from __future__ import annotations

import re
import unicodedata


def normalize_text(value: str, *, casefold: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.casefold() if casefold else normalized


def normalize_predicate(value: str) -> str:
    normalized = normalize_text(value, casefold=True).replace("-", " ")
    return re.sub(r"\s+", "_", normalized)
