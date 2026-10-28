"""Namespace-scoped access to already admitted memories for active reading."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace

from .models import MemoryStatus, RetrievalItem
from .reading.provenance import (
    EvidenceProvenanceProvider,
    MemoryServiceProvenanceProvider,
    SourceStamp,
    admitted_memory,
    complete_source_refs,
)
from .retrieval import RetrievalConfig, scoring_policy
from .service import MemoryService
from .temporal import parse_instant, require_instant


class MemoryEvidenceSource:
    def __init__(
        self,
        service: MemoryService,
        namespace: str,
        *,
        as_of: str | None = None,
        initial: Sequence[RetrievalItem] = (),
        provenance: EvidenceProvenanceProvider | None = None,
    ) -> None:
        self.service, self.namespace = service, namespace
        self.as_of = require_instant(as_of, "as_of")
        self._discovered = {i.memory.id for i in initial}
        self.search_calls = 0
        self.read_calls = 0
        self.provenance = (
            provenance
            if provenance is not None
            else MemoryServiceProvenanceProvider(service)
        )

    def annotate(self, items: Sequence[RetrievalItem]) -> tuple[RetrievalItem, ...]:
        """Annotate caller-visible clips; never expand text or trust old stamps.

        This is explicit so historical readers retain their existing behavior.
        New readers can apply the same path to initial/search/read results.
        """
        annotated = []
        cutoff = parse_instant(self.as_of)
        for item in items:
            current = admitted_memory(
                self.service, item.memory, namespace=self.namespace, as_of=self.as_of
            )
            if (
                not item.memory.content.strip()
                or item.memory.content not in current.content
            ):
                raise ValueError("visible clip is not part of the admitted memory")
            refs = complete_source_refs(current, item.source_refs)
            stamps = self.provenance.for_memory(
                current, refs, namespace=self.namespace, as_of=self.as_of
            )
            if (
                any(not isinstance(stamp, SourceStamp) for stamp in stamps)
                or len(stamps) != len(refs)
                or {stamp.source_ref for stamp in stamps} != set(refs)
            ):
                raise ValueError("source metadata does not cover current provenance")
            by_ref = {stamp.source_ref: stamp for stamp in stamps}
            visible_stamps = []
            for ref in refs:
                stamp = by_ref[ref]
                observed = parse_instant(stamp.statement_at)
                if cutoff is not None and (observed is None or observed > cutoff):
                    stamp = SourceStamp(ref)
                visible_stamps.append(stamp.to_dict())
            structured = deepcopy(current.structured_content)
            structured.pop("_reader_sources", None)
            structured["_reader_statement_sources"] = visible_stamps
            structured["role"] = "unknown"
            annotated.append(
                RetrievalItem(
                    replace(
                        current,
                        content=item.memory.content,
                        structured_content=structured,
                    ),
                    item.score,
                    list(item.channels),
                    list(item.source_refs),
                )
            )
        return tuple(annotated)

    def _item(self, memory_id: str) -> RetrievalItem:
        memory = self.service.get_memory(memory_id)
        if (
            memory is None
            or memory.namespace != self.namespace
            or memory.status
            in {
                MemoryStatus.DELETED,
                MemoryStatus.MERGED,
            }
        ):
            raise ValueError("memory is unavailable in this project")
        cutoff = parse_instant(self.as_of)
        if cutoff:
            for value in (memory.valid_from, memory.observed_at):
                parsed = parse_instant(value)
                if value and (parsed is None or parsed > cutoff):
                    raise ValueError("memory is outside the observation cutoff")
        return RetrievalItem(
            memory, 1.0, ["active_memory_read"], list(memory.source_event_ids)
        )

    def search(self, query: str) -> Sequence[RetrievalItem]:
        self.search_calls += 1
        result = self.service.retrieve(
            self.namespace,
            query,
            limit=8,
            as_of=self.as_of,
            observed_as_of=self.as_of,
            config=RetrievalConfig.full(
                include_document_chunks=True,
                name="active-memory-search-v1",
                scoring=scoring_policy("balanced"),
            ),
        )
        items = []
        for candidate in result.items:
            item = self._item(candidate.memory.id)
            self._discovered.add(item.memory.id)
            items.append(item)
        return items

    def read(self, memory_id: str) -> RetrievalItem:
        self.read_calls += 1
        if memory_id not in self._discovered:
            raise ValueError("memory must be retrieved before it can be read")
        return self._item(memory_id)
