from __future__ import annotations


PREDICATE_MAP = {
    "residence": "lives_in",
    "preference": "prefers",
    "primary_tool": "uses",
    "goal": "pursues",
}


def relation_predicate(predicate: str) -> str | None:
    return PREDICATE_MAP.get(predicate)
