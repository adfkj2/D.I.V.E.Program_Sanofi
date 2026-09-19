from __future__ import annotations

from dataclasses import dataclass

from .models import RetrievalItem


@dataclass(slots=True)
class PackedContext:
    items: list[RetrievalItem]
    text: str
    estimated_tokens: int
    omitted: int


def pack_context(items: list[RetrievalItem], token_budget: int = 1500) -> PackedContext:
    selected: list[RetrievalItem] = []
    chunks: list[str] = []
    used = 0
    for item in items:
        source = ",".join(item.source_refs) or "unknown"
        validity = f" valid={item.memory.valid_from or '?'}..{item.memory.valid_to or 'present'}"
        chunk = f"[{item.memory.id}; source={source};{validity}] {item.memory.content}"
        cost = max(1, len(chunk) // 4)
        if used + cost > token_budget:
            continue
        selected.append(item)
        chunks.append(chunk)
        used += cost
    return PackedContext(selected, "\n".join(chunks), used, len(items) - len(selected))
