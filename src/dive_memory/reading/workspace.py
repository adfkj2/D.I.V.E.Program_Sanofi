"""An ephemeral, source-preserving evidence pool for bounded reader contexts.

Only caller-supplied admitted items enter this pool. Packing is a projection:
unselected passages remain available for later packs, and every omission is
recorded. Fragment offsets refer to the admitted item's content, which can
itself be a passage; they do not claim offsets in an unseen source archive.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any, Sequence

from ..answering import EvidenceCitation
from ..document_chunks import split_document
from ..models import RetrievalItem
from ..token_budget import ApproximateTokenCounter
from .evidence import build_evidence_views, relevance_proxy, render_evidence_block
from .reader import ReaderRequest
from .snapshots import VERSION as SNAPSHOT_VERSION
from .snapshots import AdmittedSnapshot, acquisition_annotations


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class _Document:
    document_id: str
    source_id: str
    item: RetrievalItem
    batch: int


@dataclass(frozen=True, slots=True)
class _Fragment:
    fragment_id: str
    document: _Document
    start: int
    end: int

    @property
    def item(self) -> RetrievalItem:
        original = self.document.item
        return replace(
            original,
            memory=replace(
                original.memory,
                content=original.memory.content[self.start : self.end],
            ),
        )

    def describe(self) -> dict[str, Any]:
        item = self.document.item
        return {
            "fragment_id": self.fragment_id,
            "document_id": self.document.document_id,
            "source_id": self.document.source_id,
            "memory_id": item.memory.id,
            "source_refs": list(item.source_refs),
            "batch": self.document.batch,
            "start": self.start,
            "end": self.end,
            "admitted_chars": len(item.memory.content),
            "offset_basis": "admitted_content",
        }


@dataclass(frozen=True, slots=True)
class EvidencePin:
    """A literal citation anchored to a particular previously visible passage."""

    citation: EvidenceCitation
    document_id: str | None
    start: int = 0
    end: int = 0
    error: str | None = None


class BoundedEvidenceWorkspace:
    """Accumulate admitted evidence, then fairly select exact bounded passages.

    Construction does not implicitly add ``request.items``. A caller explicitly
    adds initial evidence and each later search/read result. Stable identifiers
    live in audit data; model citation indices always refer to the current pack.
    """

    def __init__(
        self,
        request: ReaderRequest,
        *,
        passage_chars: int = 800,
        identity_policy: str = "retrieval-v1",
    ) -> None:
        if type(passage_chars) is not int or not 256 <= passage_chars <= 4800:
            raise ValueError("passage_chars must be an integer between 256 and 4800")
        if type(request.token_budget) is not int or request.token_budget < 0:
            raise ValueError("token_budget must be a nonnegative integer")
        if not isinstance(identity_policy, str) or identity_policy not in {
            "retrieval-v1",
            "admitted-v1",
        }:
            raise ValueError("identity_policy must be retrieval-v1 or admitted-v1")
        self.question = request.question
        self.question_date = request.question_date
        self.token_budget = request.token_budget
        self.passage_chars = passage_chars
        self.identity_policy = identity_policy
        self._documents: dict[str, _Document] = {}
        self._fragments: dict[str, _Fragment] = {}
        self._batches = 0
        self._counter = ApproximateTokenCounter()
        self._visible_spans: dict[int, tuple[RetrievalItem, _Fragment]] = {}
        self._acquisitions: list[dict[str, Any]] = []

    def _document_id(self, item: RetrievalItem) -> str:
        return (
            AdmittedSnapshot.from_item(item).fingerprint()
            if self.identity_policy == "admitted-v1"
            else _digest(asdict(item))
        )

    def add(self, items: Sequence[RetrievalItem]) -> dict[str, Any]:
        """Snapshot and register exact passages without merging unlike evidence."""
        batch = self._batches
        self._batches += 1
        added: list[str] = []
        duplicates: list[str] = []
        acquisitions: list[dict[str, Any]] = []
        for position, supplied in enumerate(items):
            item = deepcopy(supplied)
            document_id = self._document_id(item)
            if self.identity_policy == "admitted-v1":
                acquisition = {
                    "batch": batch,
                    "position": position,
                    "document_id": document_id,
                    "duplicate": document_id in self._documents,
                    "score": item.score,
                    "channels": list(item.channels),
                    "source_refs": list(item.source_refs),
                    "decorations": acquisition_annotations(item),
                }
                self._acquisitions.append(acquisition)
                acquisitions.append(acquisition)
            if document_id in self._documents:
                duplicates.append(document_id)
                continue
            # Extracted observations and a source document can have different
            # memory IDs while coming from the same event. They share a fair
            # packing slot; distinct snapshots and their qualifiers stay intact.
            source_id = _digest(
                [
                    item.memory.namespace,
                    sorted(set(item.source_refs))
                    if item.source_refs
                    else [item.memory.id],
                    bool(item.source_refs),
                ]
            )
            document = _Document(document_id, source_id, item, batch)
            self._documents[document_id] = document
            added.append(document_id)
            for chunk in split_document(
                item.memory.content,
                max_chars=self.passage_chars,
                overlap_chars=min(80, self.passage_chars // 8),
            ):
                fragment_id = _digest([document_id, chunk.start, chunk.end])
                self._fragments[fragment_id] = _Fragment(
                    fragment_id, document, chunk.start, chunk.end
                )
        audit = {
            "batch": batch,
            "added_document_ids": added,
            "duplicate_document_ids": duplicates,
            "registered_documents": len(self._documents),
            "registered_fragments": len(self._fragments),
        }
        if self.identity_policy == "admitted-v1":
            audit.update(
                identity_policy=self.identity_policy,
                acquisitions=deepcopy(acquisitions),
            )
        return audit

    def _render(self, items: Sequence[RetrievalItem]) -> str:
        return render_evidence_block(
            build_evidence_views(
                items, question=self.question, question_date=self.question_date
            )
        )

    def _ordered_fragments(self) -> list[_Fragment]:
        groups: dict[str, list[_Fragment]] = defaultdict(list)
        for fragment in self._fragments.values():
            groups[fragment.document.source_id].append(fragment)
        batches: dict[int, list[str]] = defaultdict(list)
        for source_id, fragments in groups.items():
            first_batch = min(fragment.document.batch for fragment in fragments)
            batches[first_batch].append(source_id)
        batch_queues = [deque(values) for values in batches.values()]
        source_order: list[str] = []
        while any(batch_queues):
            for queue in batch_queues:
                if queue:
                    source_order.append(queue.popleft())
        queues: list[deque[_Fragment]] = []
        for source_id in source_order:
            fragments = sorted(
                groups[source_id],
                key=lambda fragment: (
                    -relevance_proxy(
                        self.question,
                        fragment.item.memory.content,
                        fragment.document.item.channels,
                    ),
                    fragment.document.batch,
                    fragment.start,
                    fragment.fragment_id,
                ),
            )
            queues.append(deque(fragments))
        ordered: list[_Fragment] = []
        while any(queues):
            for fragment_queue in queues:
                if fragment_queue:
                    ordered.append(fragment_queue.popleft())
        return ordered

    def anchor(
        self,
        citations: Sequence[EvidenceCitation],
        visible: Sequence[RetrievalItem],
        *,
        indices: Sequence[int] | None = None,
    ) -> tuple[EvidencePin, ...]:
        """Bind quotes to their visible passages before another pack changes order.

        An exact original model index can be supplied. Without indices, multiple
        matching visible passages are all pinned: no unseen occurrence or guessed
        heading is substituted. Within one passage a repeated quote retains that
        entire passage, because the literal quote cannot identify an occurrence.
        """
        if indices is not None and len(indices) != len(citations):
            raise ValueError("citation indices must match the citations")
        pins: list[EvidencePin] = []
        for position, citation in enumerate(citations):
            candidates = list(visible)
            if indices is not None:
                index = indices[position]
                candidates = (
                    [visible[index]]
                    if type(index) is int and 0 <= index < len(visible)
                    else []
                )
            matches: list[EvidencePin] = []
            for item in candidates:
                if (
                    item.memory.id != citation.memory_id
                    or not citation.quote.strip()
                    or citation.quote not in item.memory.content
                    or not citation.source_refs
                    or not set(citation.source_refs) <= set(item.source_refs)
                ):
                    continue
                packed = self._visible_spans.get(id(item))
                if packed is not None and packed[0] is item:
                    fragment = packed[1]
                    # Reject mutations made after the pack was returned.
                    if asdict(item) != asdict(fragment.item):
                        continue
                else:
                    document = self._documents.get(self._document_id(item))
                    if document is None:
                        continue
                    fragment = _Fragment(
                        _digest([document.document_id, 0, len(item.memory.content)]),
                        document,
                        0,
                        len(item.memory.content),
                    )
                matches.append(
                    EvidencePin(
                        citation,
                        fragment.document.document_id,
                        fragment.start,
                        fragment.end,
                    )
                )
            pins.extend(
                matches
                or [EvidencePin(citation, None, error="pin_visible_anchor_missing")]
            )
        return tuple(pins)

    def _pin(self, pin: EvidenceCitation | EvidencePin) -> _Fragment | None:
        citation = pin.citation if isinstance(pin, EvidencePin) else pin
        if not citation.quote.strip() or not citation.source_refs:
            return None
        documents = list(self._documents.values())
        if isinstance(pin, EvidencePin):
            if pin.error or pin.document_id not in self._documents:
                return None
            documents = [self._documents[pin.document_id]]
        for document in documents:
            item = document.item
            if item.memory.id != citation.memory_id or not set(
                citation.source_refs
            ) <= set(item.source_refs):
                continue
            left, right = (
                (pin.start, pin.end)
                if isinstance(pin, EvidencePin)
                else (0, len(item.memory.content))
            )
            if not 0 <= left < right <= len(item.memory.content):
                continue
            content = item.memory.content[left:right]
            start = content.find(citation.quote)
            if start < 0:
                continue
            if (
                isinstance(pin, EvidencePin) and len(content) <= self.passage_chars
            ) or content.find(citation.quote, start + 1) >= 0:
                # Exact text alone cannot choose between repeated occurrences.
                # A normal visible passage also retains its heading and local
                # qualifiers rather than reducing them to a bare quoted claim.
                window_start, window_end = left, right
            else:
                window_chars = min(len(content), max(len(citation.quote) + 160, 240))
                window_left = max(0, start - (window_chars - len(citation.quote)) // 2)
                window_right = min(len(content), window_left + window_chars)
                window_left = max(0, window_right - window_chars)
                window_start, window_end = left + window_left, left + window_right
            fragment_id = _digest([document.document_id, window_start, window_end])
            return _Fragment(fragment_id, document, window_start, window_end)
        return None

    def pack(
        self, *, pins: Sequence[EvidenceCitation | EvidencePin] = ()
    ) -> tuple[tuple[RetrievalItem, ...], dict[str, Any]]:
        """Pack pins first, then interleave acquisition rounds and sources.

        A failed pin is an explicit ``pin_errors`` entry. Callers must reject a
        review using such a pack rather than silently accepting a lost premise.
        The reported budget is the repository's approximate context-token
        budget, measured with the same evidence renderer used by the reader.
        """
        selected: list[_Fragment] = []
        selections: list[dict[str, Any]] = []
        omitted: list[dict[str, Any]] = []
        pin_errors: list[dict[str, Any]] = []
        pinned: list[_Fragment] = []

        def fits(fragment: _Fragment) -> bool:
            return (
                self._counter.count(
                    self._render([f.item for f in [*selected, fragment]])
                )
                <= self.token_budget
            )

        requested_pins: list[_Fragment] = []
        for pin in pins:
            citation = pin.citation if isinstance(pin, EvidencePin) else pin
            fragment = self._pin(pin)
            if fragment is None:
                pin_errors.append(
                    {
                        "memory_id": citation.memory_id,
                        "reason": pin.error
                        if isinstance(pin, EvidencePin) and pin.error
                        else "pin_not_admitted",
                    }
                )
                continue
            requested_pins.append(fragment)
        # Coalesce overlapping source windows before counting their metadata.
        # Every resulting window remains one exact slice of the same snapshot.
        merged_pins: list[_Fragment] = []
        for fragment in sorted(
            requested_pins,
            key=lambda value: (value.document.document_id, value.start, value.end),
        ):
            if (
                merged_pins
                and merged_pins[-1].document.document_id
                == fragment.document.document_id
                and fragment.start <= merged_pins[-1].end
            ):
                previous = merged_pins.pop()
                end = max(previous.end, fragment.end)
                fragment = _Fragment(
                    _digest([previous.document.document_id, previous.start, end]),
                    previous.document,
                    previous.start,
                    end,
                )
            merged_pins.append(fragment)
        for fragment in merged_pins:
            if not fits(fragment):
                pin_errors.append({**fragment.describe(), "reason": "pin_budget"})
                continue
            selected.append(fragment)
            pinned.append(fragment)
            selections.append({**fragment.describe(), "reason": "pinned_citation"})

        for fragment in self._ordered_fragments():
            if any(
                pin.document.document_id == fragment.document.document_id
                and pin.start <= fragment.start
                and pin.end >= fragment.end
                for pin in pinned
            ):
                omitted.append({**fragment.describe(), "reason": "covered_by_pin"})
            elif fits(fragment):
                selected.append(fragment)
                selections.append(
                    {**fragment.describe(), "reason": "source_round_robin"}
                )
            else:
                omitted.append({**fragment.describe(), "reason": "context_budget"})
        items = tuple(deepcopy(fragment.item) for fragment in selected)
        for item, fragment in zip(items, selected, strict=True):
            self._visible_spans[id(item)] = (item, fragment)
        for index, entry in enumerate(selections):
            entry["index"] = index
        audit = {
            "version": "bounded-evidence-workspace-v1",
            "token_counter": self._counter.name,
            "token_counter_exact": self._counter.exact,
            "context_tokens": self._counter.count(self._render(items)),
            "pinned_context_tokens": self._counter.count(
                self._render([f.item for f in pinned])
            ),
            "token_budget": self.token_budget,
            "registered_documents": len(self._documents),
            "registered_fragments": len(self._fragments),
            "registered": [
                {
                    "document_id": d.document_id,
                    "source_id": d.source_id,
                    "memory_id": d.item.memory.id,
                    "source_refs": list(d.item.source_refs),
                    "batch": d.batch,
                    "admitted_chars": len(d.item.memory.content),
                }
                for d in self._documents.values()
            ],
            "registered_source_ids": list(
                dict.fromkeys(d.source_id for d in self._documents.values())
            ),
            "visible_source_ids": list(
                dict.fromkeys(f.document.source_id for f in selected)
            ),
            "source_grouping": "namespace_and_identical_source_ref_set",
            "visible_source_refs": sorted(
                {ref for item in items for ref in item.source_refs}
            ),
            "visible_batches": dict(
                sorted(
                    (batch, sum(f.document.batch == batch for f in selected))
                    for batch in {d.batch for d in self._documents.values()}
                )
            ),
            "selected": selections,
            "omitted": omitted,
            "pin_errors": pin_errors,
            "empty_document_ids": [
                d.document_id
                for d in self._documents.values()
                if not d.item.memory.content
            ],
        }
        if self.identity_policy == "admitted-v1":
            audit.update(
                version="bounded-evidence-workspace-admitted-v1",
                identity_policy=self.identity_policy,
                snapshot_version=SNAPSHOT_VERSION,
                acquisitions=deepcopy(self._acquisitions),
            )
        return items, audit
