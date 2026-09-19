from __future__ import annotations

import re


def normalize_entity(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip().lower())
    return value.strip(".,，。!?！？")


def entity_candidates(content: str, structured: dict) -> list[tuple[str, str]]:
    predicate = str(structured.get("predicate", "statement"))
    value = str(structured.get("value", content))
    if not value:
        return []
    entity_type = "location" if predicate == "residence" else "concept"
    return [(normalize_entity(value), entity_type)]
