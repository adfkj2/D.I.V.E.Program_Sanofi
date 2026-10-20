"""Controlled document supplement shared by the runner and offline experiments."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..context import PackedContext, pack_context
from ..models import DOCUMENT_PREDICATE, RetrievalItem
from ..retrieval import RetrievalConfig, scoring_policy
from ..token_budget import ApproximateTokenCounter
from .evidence import build_evidence_views, render_evidence_block

SCORING_POLICY = "balanced"
TOP_K = 10
TOKEN_BUDGET = 1500
CONTROLLED_DOCUMENT_LIMIT = 2
CONTROLLED_DOCUMENT_TOKEN_BUDGET = 600


def _document_supplement_config() -> RetrievalConfig:
    """A side lane that can nominate documents but cannot vote in baseline RRF."""

    return RetrievalConfig(
        enabled_channels=frozenset({"document_chunk", "temporal"}),
        name="oracle-document-ER-supplement-only",
        scoring=scoring_policy(SCORING_POLICY),
    )


def _is_document(item: RetrievalItem) -> bool:
    return str((item.memory.structured_content or {}).get("predicate", "")) == DOCUMENT_PREDICATE


def _controlled_candidates(
    baseline_items: Sequence[RetrievalItem],
    supplement_items: Sequence[RetrievalItem],
    *,
    max_documents: int = CONTROLLED_DOCUMENT_LIMIT,
    limit: int = TOP_K,
    prefer_explicit_user_documents: bool = False,
) -> tuple[list[RetrievalItem], list[RetrievalItem], dict[str, Any]]:
    """Protect every baseline non-document and nominate at most two documents.

    The first document is the highest baseline document that also has a chunk
    hit (falling back to the highest baseline document).  Remaining capacity is
    filled from chunk-only rank, then from any remaining baseline document.
    Scores from the side lane are never fused with baseline scores.
    """

    non_documents = [item for item in baseline_items if not _is_document(item)]
    baseline_documents = [item for item in baseline_items if _is_document(item)]
    supplement_documents = [item for item in supplement_items if _is_document(item)]
    supplement_ids = {item.memory.id for item in supplement_documents}
    selected: list[RetrievalItem] = []
    seen: set[str] = set()
    capacity = min(max(0, int(max_documents)), max(0, limit - len(non_documents)))

    common = [item for item in baseline_documents if item.memory.id in supplement_ids]
    primary = (common or baseline_documents or supplement_documents)[:1]
    explicit = []
    if prefer_explicit_user_documents:
        from ..user_document_retention import VERSION
        explicit = [item for item in supplement_documents
                    if item.memory.structured_content.get('role') == 'user'
                    and item.memory.structured_content.get('retention_policy') == VERSION][:1]
    for item in [*explicit, *primary, *supplement_documents, *baseline_documents]:
        if len(selected) >= capacity:
            break
        if item.memory.id in seen:
            continue
        selected.append(item)
        seen.add(item.memory.id)
        if len(selected) >= max(0, int(max_documents)):
            break
    return non_documents, selected, {
        "strategy": ("explicit_user_document_then_baseline_common_and_chunk_rescue"
                     if prefer_explicit_user_documents else "baseline_common_first_then_chunk_rescue"),
        "protected_non_document_ids": [item.memory.id for item in non_documents],
        "baseline_document_ids": [item.memory.id for item in baseline_documents],
        "supplement_ranked_document_ids": [item.memory.id for item in supplement_documents],
        "selected_document_ids": [item.memory.id for item in selected],
        "max_document_candidates": max_documents,
    }


def _controlled_pack(
    non_documents: Sequence[RetrievalItem],
    documents: Sequence[RetrievalItem],
    *,
    question: str,
    question_date: str,
    current_only: bool,
    document_spans: Mapping[str, tuple[int, int]],
    token_budget: int = TOKEN_BUDGET,
    document_token_budget: int = CONTROLLED_DOCUMENT_TOKEN_BUDGET,
    max_documents: int = CONTROLLED_DOCUMENT_LIMIT,
    document_char_budget: int | None = None,
    expand_document_spans: bool = False,
) -> tuple[PackedContext, dict[str, Any]]:
    """Pack protected facts first; documents may consume only leftover budget.

    The document lane is independently budgeted using the exact annotated
    evidence renderer.  A document is appended only if the combined context is
    still within the global budget, so no already-packed non-document can be
    displaced by the supplement.
    """

    counter = ApproximateTokenCounter()

    def render(items: Sequence[RetrievalItem]) -> str:
        return render_evidence_block(build_evidence_views(
            items, question=question, question_date=question_date,
        ))

    non_document_pack = pack_context(
        list(non_documents), max(0, int(token_budget)), current_only=current_only,
        question=question, document_selection="query_excerpt", render_for_budget=render,
    )
    document_pack = pack_context(
        list(documents), max(0, int(document_token_budget)), current_only=current_only,
        content_caps={DOCUMENT_PREDICATE: (scoring_policy(SCORING_POLICY).document_char_budget
                      if document_char_budget is None else document_char_budget)},
        question=question, document_selection="query_excerpt",
        document_spans=document_spans, expand_document_spans=expand_document_spans, render_for_budget=render,
    )

    selected = list(non_document_pack.items)
    visible = list(non_document_pack.visible_items)
    decisions = [{**row, "lane": "protected_non_document"}
                 for row in non_document_pack.decisions]
    appended_documents: list[str] = []
    for original, shown in zip(document_pack.items, document_pack.visible_items, strict=True):
        proposed = render([*visible, shown])
        if counter.count(proposed) > token_budget:
            decisions.append({
                "memory_id": original.memory.id,
                "decision": "EXCLUDE",
                "reason": "controlled_global_budget_after_protected_non_documents",
                "lane": "document_supplement",
                "candidate_tokens": counter.count(render([shown])),
                "budget": token_budget,
            })
            continue
        selected.append(original)
        visible.append(shown)
        appended_documents.append(original.memory.id)
        decisions.append({
            "memory_id": original.memory.id,
            "decision": "INCLUDE",
            "reason": "controlled_document_supplement",
            "lane": "document_supplement",
            "cumulative_tokens": counter.count(proposed),
        })

    # Preserve the document-lane truncation and quota exclusions for audit,
    # without duplicating its INCLUDE rows (the combined-budget decision above
    # is the authoritative inclusion decision).
    decisions.extend(
        {**row, "lane": "document_supplement"}
        for row in document_pack.decisions
        if row.get("decision") != "INCLUDE"
    )
    text = render(visible)
    document_visible = [item for item in visible if _is_document(item)]
    document_tokens = counter.count(render(document_visible)) if document_visible else 0
    if len(document_visible) > max_documents:
        raise RuntimeError("controlled arm exceeded its document count limit")
    if document_tokens > document_token_budget:
        raise RuntimeError("controlled arm exceeded its independent document token budget")
    if counter.count(text) > token_budget:
        raise RuntimeError("controlled arm exceeded the global evidence budget")
    packed = PackedContext(
        items=selected,
        visible_items=visible,
        text=text,
        estimated_tokens=counter.count(text),
        omitted=len(non_documents) + len(documents) - len(selected),
        decisions=decisions,
        token_counter=counter.name,
        token_count_degraded=not counter.exact,
    )
    return packed, {
        "max_documents": max_documents,
        "document_char_budget": document_char_budget,
        "expand_document_spans": expand_document_spans,
        "document_token_budget": document_token_budget,
        "document_context_tokens": document_tokens,
        "document_candidates": len(documents),
        "documents_packed": len(document_visible),
        "appended_document_ids": appended_documents,
        "protected_non_documents_packed": len(non_document_pack.items),
        "protected_non_documents_omitted_by_global_budget": non_document_pack.omitted,
    }
