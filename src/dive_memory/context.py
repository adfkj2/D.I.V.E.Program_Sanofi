from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import MemoryStatus, RetrievalItem
from .normalization import normalize_text
from .token_budget import ApproximateTokenCounter, TokenCounter


@dataclass(slots=True)
class PackedContext:
    items: list[RetrievalItem]
    text: str
    estimated_tokens: int
    omitted: int
    decisions: list[dict[str, Any]] = field(default_factory=list)
    token_counter: str = "utf8-character-heuristic-v1"
    token_count_degraded: bool = True


def _canonical_key(item: RetrievalItem) -> tuple[str, str, str]:
    structured = item.memory.structured_content
    return (
        normalize_text(str(structured.get("subject", "user")), casefold=True),
        normalize_text(str(structured.get("predicate", "statement")), casefold=True),
        normalize_text(str(structured.get("normalized_value", structured.get("value", item.memory.content))),
                       casefold=True),
    )


def _deduplicate(items: list[RetrievalItem], decisions: list[dict[str, Any]]) -> list[RetrievalItem]:
    unique: list[RetrievalItem] = []
    positions: dict[tuple[str, str, str], int] = {}
    for item in items:
        key = _canonical_key(item)
        if key not in positions:
            positions[key] = len(unique)
            unique.append(RetrievalItem(
                item.memory, item.score, list(dict.fromkeys(item.channels)),
                list(dict.fromkeys(item.source_refs)),
            ))
            continue
        survivor = unique[positions[key]]
        survivor.score = max(survivor.score, item.score)
        survivor.channels = list(dict.fromkeys([*survivor.channels, *item.channels]))
        survivor.source_refs = list(dict.fromkeys([*survivor.source_refs, *item.source_refs]))
        decisions.append({
            "memory_id": item.memory.id,
            "decision": "EXCLUDE",
            "reason": "canonical_duplicate",
            "merged_into": survivor.memory.id,
        })
    return unique


def pack_context(
    items: list[RetrievalItem],
    token_budget: int = 1500,
    *,
    token_counter: TokenCounter | None = None,
    current_only: bool = True,
) -> PackedContext:
    budget = max(0, int(token_budget))
    counter = token_counter or ApproximateTokenCounter()
    decisions: list[dict[str, Any]] = []
    eligible: list[RetrievalItem] = []
    for item in items:
        if item.memory.status in {MemoryStatus.DELETED, MemoryStatus.MERGED}:
            decisions.append({"memory_id": item.memory.id, "decision": "EXCLUDE", "reason": "terminal_status"})
            continue
        if current_only and item.memory.status in {MemoryStatus.SUPERSEDED, MemoryStatus.ARCHIVED}:
            decisions.append({
                "memory_id": item.memory.id, "decision": "EXCLUDE",
                "reason": "outdated_for_current_query",
            })
            continue
        eligible.append(item)
    candidates = _deduplicate(eligible, decisions)

    contradiction_roots: dict[str, str] = {}
    for item in candidates:
        if item.memory.contradicts_id:
            root = item.memory.contradicts_id
            contradiction_roots[item.memory.id] = root
            contradiction_roots[root] = root

    selected: list[RetrievalItem] = []
    chunks: list[str] = []
    for item in candidates:
        source = ",".join(item.source_refs) or "unknown"
        validity = f"valid={item.memory.valid_from or '?'}..{item.memory.valid_to or 'present'}"
        conflict = contradiction_roots.get(item.memory.id)
        conflict_label = f"; conflict_group={conflict}" if conflict else ""
        chunk = f"[{item.memory.id}; source={source}; {validity}{conflict_label}] {item.memory.content}"
        proposed_text = "\n".join([*chunks, chunk])
        proposed_tokens = counter.count(proposed_text)
        if proposed_tokens > budget:
            decisions.append({
                "memory_id": item.memory.id, "decision": "EXCLUDE", "reason": "token_budget",
                "candidate_tokens": counter.count(chunk), "budget": budget,
            })
            continue
        selected.append(item)
        chunks.append(chunk)
        decisions.append({
            "memory_id": item.memory.id, "decision": "INCLUDE", "reason": "ranked_within_budget",
            "cumulative_tokens": proposed_tokens,
        })

    text = "\n".join(chunks)
    return PackedContext(
        selected,
        text,
        counter.count(text),
        len(items) - len(selected),
        decisions,
        counter.name,
        not counter.exact,
    )
