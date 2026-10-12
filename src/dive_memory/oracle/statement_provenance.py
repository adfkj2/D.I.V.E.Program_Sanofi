"""Reader-only statement clocks; never infer an event date from a message date."""
from dataclasses import replace
from typing import Any, Mapping, Sequence

from ..models import RetrievalItem


def attach_statement_provenance(
    items: Sequence[RetrievalItem], sources: Mapping[str, Mapping[str, Any]],
) -> list[RetrievalItem]:
    result = []
    for item in items:
        # Include missing refs so partial provenance cannot imply a single known
        # speaker/time for the whole item. Whitelist fields at the boundary.
        records = [{"source_ref": ref, "role": sources.get(ref, {}).get("role") or "unknown",
                    "statement_at": sources.get(ref, {}).get("statement_at")}
                   for ref in dict.fromkeys(item.source_refs)]
        structured = dict(item.memory.structured_content or {})
        structured["_reader_statement_sources"] = records
        result.append(RetrievalItem(replace(item.memory, structured_content=structured),
                                    item.score, list(item.channels), list(item.source_refs)))
    return result
