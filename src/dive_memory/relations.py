from __future__ import annotations


PREDICATE_MAP = {
    "residence": "lives_in",
    "preference": "prefers",
    "primary_tool": "uses",
    "goal": "pursues",
}

# Predicates whose value is single-valued for a subject: a newer statement
# replaces the older one ("我住在成都" -> "我住在上海").
#
# Everything else accumulates. docs/04 is explicit: supersession applies only
# "在 predicate 相同或规则明确时", otherwise "两个事实并存". Treating every
# predicate as single-valued made two unrelated statements supersede each other,
# and dropped one of two coexisting preferences.
SINGLE_VALUED_PREDICATES = frozenset({"residence", "primary_tool", "goal"})


def relation_predicate(predicate: str) -> str | None:
    return PREDICATE_MAP.get(predicate)


def is_single_valued(predicate: object) -> bool:
    return isinstance(predicate, str) and predicate in SINGLE_VALUED_PREDICATES
