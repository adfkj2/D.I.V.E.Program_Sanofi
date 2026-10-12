"""Budgeted source-role/turn coordinates, derived only from ingested events.

Does not modify persistent memories, ranks, source ids or source text. Multiple
provenances remain explicit rather than being silently labelled as one speaker.
"""
from dataclasses import replace
from typing import Any, Mapping, Sequence

from ..models import RetrievalItem


def attach_source_coordinates(items: Sequence[RetrievalItem],
                              positions: Mapping[str, Mapping[str, Any]]) -> list[RetrievalItem]:
    result = []
    for item in items:
        coords = [dict(positions[ref]) for ref in item.source_refs if ref in positions]
        structured = dict(item.memory.structured_content or {})
        if coords:
            structured["_reader_sources"] = coords
        result.append(RetrievalItem(replace(item.memory, structured_content=structured),
                                    item.score, list(item.channels), list(item.source_refs)))
    return result
