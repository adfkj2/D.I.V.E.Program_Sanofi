"""Favor distinct provenance before spending slots on the same source again."""
from __future__ import annotations


def diversify_sources(items, limit):
    chosen, rest, seen = [], [], set()
    for item in items:
        # Missing provenance is never conflated across unrelated memories.
        keys = set(item.source_refs) or {"memory:" + item.memory.id}
        if keys - seen:
            chosen.append(item)
            seen.update(keys)
        else:
            rest.append(item)
    result = (chosen + rest)[:max(0, limit)]
    return result, {"version": "source-diversity-v1", "pool_size": len(items),
                    "selected_ids": [i.memory.id for i in result],
                    "distinct_source_first": True}
