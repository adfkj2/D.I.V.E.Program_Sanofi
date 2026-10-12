from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from .models import DOCUMENT_PREDICATE, MemoryStatus, RetrievalItem
from .normalization import normalize_text
from .token_budget import ApproximateTokenCounter, TokenCounter


@dataclass(slots=True)
class PackedContext:
    items: list[RetrievalItem]
    # Source memories remain intact in ``items``. These copies contain only the
    # verbatim spans the reader is actually allowed to see and cite.
    visible_items: list[RetrievalItem]
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
        # Reader-only provenance annotations must follow merged source refs.
        source_positions = [*(survivor.memory.structured_content.get("_reader_sources") or []),
                     *(item.memory.structured_content.get("_reader_sources") or [])]
        if source_positions:
            merged = {position["source_ref"]: position for position in source_positions}
            survivor.memory = replace(survivor.memory, structured_content={
                **survivor.memory.structured_content, "_reader_sources": list(merged.values()),
            })
        statement_sources = [*(survivor.memory.structured_content.get("_reader_statement_sources") or []),
                             *(item.memory.structured_content.get("_reader_statement_sources") or [])]
        if statement_sources:
            merged_statements = {source["source_ref"]: source for source in statement_sources}
            # Render ordinals in source_refs order, keeping missing provenance.
            sources = [merged_statements.get(ref, {"source_ref": ref, "role": "unknown", "statement_at": None})
                       for ref in survivor.source_refs]
            survivor.memory = replace(survivor.memory, structured_content={
                **survivor.memory.structured_content, "_reader_statement_sources": sources,
            })
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
    content_caps: Mapping[str, int] | None = None,
    question: str | None = None,
    document_selection: str = "prefix",
    document_spans: Mapping[str, tuple[int, int]] | None = None,
    expand_document_spans: bool = False,
    render_for_budget: Callable[[Sequence[RetrievalItem]], str] | None = None,
) -> PackedContext:
    """Select and render the context the reader will see.

    ``content_caps`` maps a predicate to a character ceiling. With the default
    ``prefix`` policy the historical flat text is unchanged. ``query_excerpt``
    instead selects a source-verbatim passage from a document; it is explicitly
    opt-in so old benchmark runs are not silently reinterpreted. When the
    reader annotates evidence, ``render_for_budget`` must render the *actual*
    reader context so its metadata is accounted for in the same token budget.
    """
    if document_selection not in {"prefix", "query_excerpt"}:
        raise ValueError("document_selection must be 'prefix' or 'query_excerpt'")
    if document_selection == "query_excerpt" and question is None:
        raise ValueError("query_excerpt requires a question")
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
    visible_items: list[RetrievalItem] = []
    chunks: list[str] = []
    for item in candidates:
        source = ",".join(item.source_refs) or "unknown"
        validity = f"valid={item.memory.valid_from or '?'}..{item.memory.valid_to or 'present'}"
        conflict = contradiction_roots.get(item.memory.id)
        conflict_label = f"; conflict_group={conflict}" if conflict else ""
        source_content = item.memory.content
        visible_content = source_content
        displayed_content = source_content
        predicate = str((item.memory.structured_content or {}).get("predicate", ""))
        cap = int((content_caps or {}).get(predicate, 0))
        if cap and len(source_content) > cap:
            span_start = 0
            span_end = cap
            excerpt_selected = document_selection == "query_excerpt" and predicate == DOCUMENT_PREDICATE
            selection_source = "prefix"
            if excerpt_selected:
                indexed_span = (document_spans or {}).get(item.memory.id)
                if (indexed_span is not None and len(indexed_span) == 2 and
                        0 <= indexed_span[0] < indexed_span[1] <= len(source_content) and
                        indexed_span[1] - indexed_span[0] <= cap):
                    span_start, span_end = indexed_span
                    selection_source = "indexed_chunk"
                else:
                    from .document_chunks import select_document_chunk

                    passage = select_document_chunk(source_content, question or "", cap)
                    span_start, span_end = passage.start, passage.end
                    selection_source = "query_overlap"
                if expand_document_spans:
                    from .document_chunks import expand_document_span
                    passage = expand_document_span(source_content, span_start, span_end, cap)
                    span_start, span_end = passage.start, passage.end
                    selection_source += "_expanded"
            visible_content = source_content[span_start:span_end]
            displayed_content = visible_content.rstrip() + " ...[truncated to fit the context budget]"
            decisions.append({
                "memory_id": item.memory.id, "decision": "TRUNCATE",
                "reason": "query_excerpt" if excerpt_selected else "content_cap",
                "predicate": predicate, "cap": cap,
                "original_chars": len(source_content),
                "visible_start": span_start, "visible_end": span_end,
                "selection_source": selection_source,
            })
        visible = RetrievalItem(
            replace(item.memory, content=visible_content), item.score,
            list(item.channels), list(item.source_refs),
        )
        chunk = f"[{item.memory.id}; source={source}; {validity}{conflict_label}] {displayed_content}"
        proposed_text = (render_for_budget([*visible_items, visible]) if render_for_budget
                         else "\n".join([*chunks, chunk]))
        proposed_tokens = counter.count(proposed_text)
        if proposed_tokens > budget:
            decisions.append({
                "memory_id": item.memory.id, "decision": "EXCLUDE", "reason": "token_budget",
                "candidate_tokens": counter.count(render_for_budget([visible]) if render_for_budget
                                                  else chunk), "budget": budget,
            })
            continue
        selected.append(item)
        visible_items.append(visible)
        chunks.append(chunk)
        decisions.append({
            "memory_id": item.memory.id, "decision": "INCLUDE", "reason": "ranked_within_budget",
            "cumulative_tokens": proposed_tokens,
        })

    text = render_for_budget(visible_items) if render_for_budget else "\n".join(chunks)
    return PackedContext(
        selected,
        visible_items,
        text,
        counter.count(text),
        len(items) - len(selected),
        decisions,
        counter.name,
        not counter.exact,
    )
