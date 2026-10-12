"""Expand retrieved documents without replacing any already visible evidence."""
from dataclasses import replace
from typing import Any, Mapping, Sequence

from ..context import PackedContext, pack_context
from ..document_chunks import expand_document_span
from ..models import DOCUMENT_PREDICATE, RetrievalItem
from ..retrieval import scoring_policy
from ..token_budget import ApproximateTokenCounter
from .controlled_documents import _is_document
from .evidence import build_evidence_views, render_evidence_block


def extend_documents(
    packed: PackedContext, documents: Sequence[RetrievalItem], *, question: str,
    question_date: str, current_only: bool, document_spans: Mapping[str, tuple[int, int]],
    token_budget: int, max_documents: int, document_token_budget: int = 2400,
    max_chars: int = 4800, base_char_budget: int | None = None,
) -> tuple[PackedContext, dict[str, Any]]:
    """Start with the complete baseline pack; use spare budget monotonically.

    The source windows remain contiguous verbatim slices. New document snippets
    are appended before expansion so the first document cannot crowd out the
    second snippet. No candidate outside the original retrieved pool is used.
    """
    if document_token_budget < 0 or max_chars <= 0:
        raise ValueError("invalid spare document limits")
    counter = ApproximateTokenCounter()

    def render(items):
        return render_evidence_block(build_evidence_views(items, question=question, question_date=question_date))

    selected = list(packed.items)
    shown = list(packed.visible_items)
    decisions = list(packed.decisions)
    baseline = {item.memory.id: item for item in shown}
    starts = {d['memory_id']: (d['visible_start'], d['visible_end']) for d in decisions
              if d.get('decision') == 'TRUNCATE' and 'visible_start' in d}
    eligible = {d['memory_id'] for d in decisions
                if d.get('lane') == 'document_supplement' and d.get('reason') == 'token_budget'}
    additions, expansions = [], []

    def fits(items):
        docs = [item for item in items if _is_document(item)]
        return (len(docs) <= max_documents and counter.count(render(items)) <= token_budget
                and counter.count(render(docs)) <= document_token_budget)

    # A caller may not reinterpret a baseline that already violates its bounds.
    if not fits(shown):
        raise ValueError("baseline does not fit the spare-stage limits")
    base_cap = (scoring_policy('balanced').document_char_budget
                if base_char_budget is None else base_char_budget)
    for doc in documents:
        if doc.memory.id in baseline or doc.memory.id not in eligible:
            continue
        one = pack_context([doc], document_token_budget, current_only=current_only,
            question=question, document_selection='query_excerpt', document_spans=document_spans,
            content_caps={DOCUMENT_PREDICATE: base_cap}, render_for_budget=render)
        if not one.visible_items or not fits([*shown, one.visible_items[0]]):
            continue
        selected.append(one.items[0])
        shown.append(one.visible_items[0])
        for d in one.decisions:
            if d.get('decision') == 'TRUNCATE':
                starts[d['memory_id']] = (d['visible_start'], d['visible_end'])
        additions.append(doc.memory.id)
        decisions.append({'memory_id': doc.memory.id, 'decision': 'INCLUDE',
                          'reason': 'spare_document_quota', 'lane': 'document_supplement'})

    originals = {item.memory.id: item for item in selected}
    for index, item in enumerate(shown):
        if not _is_document(item):
            continue
        text = originals[item.memory.id].memory.content
        start, end = starts.get(item.memory.id, (0, len(text)))
        if text[start:end] != item.memory.content:
            raise ValueError("visible document span does not match its recorded source slice")
        largest = min(max_chars, len(text))
        for cap in range(largest, end - start, -256):
            span = expand_document_span(text, start, end, cap)
            assert span.start <= start <= end <= span.end
            if len(span.content) <= len(item.memory.content):
                continue
            candidate = replace(item, memory=replace(item.memory, content=span.content))
            proposed = [*shown[:index], candidate, *shown[index + 1:]]
            if not fits(proposed):
                continue
            shown = proposed
            change = {'memory_id': item.memory.id, 'decision': 'EXPAND',
                'reason': 'spare_budget_preserves_previous_span', 'lane': 'document_supplement',
                'previous_start': start, 'previous_end': end,
                'visible_start': span.start, 'visible_end': span.end,
                'original_chars': len(text), 'cap': cap}
            decisions.append(change)
            expansions.append(change)
            break

    for index, original in enumerate(packed.visible_items):
        current = shown[index]
        assert current.memory.id == original.memory.id
        assert current.source_refs == original.source_refs
        assert original.memory.content in current.memory.content
        if not _is_document(original):
            assert current == original
    text = render(shown)
    result = PackedContext(selected, shown, text, counter.count(text),
        max(0, packed.omitted - len(additions)), decisions, counter.name, not counter.exact)
    return result, {'strategy': 'preserve_baseline_then_spare_documents_v1',
        'baseline_visible_items': len(baseline), 'baseline_visible_preserved': True,
        'added_document_ids': additions, 'expanded_document_count': len(expansions),
        'document_token_budget': document_token_budget, 'max_chars': max_chars,
        'document_context_tokens': counter.count(render([item for item in shown if _is_document(item)])),
        'global_context_tokens': result.estimated_tokens,
        'new_visible_characters': sum(len(item.memory.content) for item in shown)
                                 - sum(len(item.memory.content) for item in packed.visible_items)}
