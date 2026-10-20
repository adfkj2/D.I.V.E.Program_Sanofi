"""Namespace-scoped access to already admitted memories for active reading."""

from collections.abc import Sequence

from .models import MemoryStatus, RetrievalItem
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
    ) -> None:
        self.service, self.namespace = service, namespace
        self.as_of = require_instant(as_of, "as_of")
        self._discovered = {i.memory.id for i in initial}
        self.search_calls = 0
        self.read_calls = 0

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
