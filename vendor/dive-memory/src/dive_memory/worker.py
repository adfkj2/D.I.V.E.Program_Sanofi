from __future__ import annotations

from dataclasses import dataclass, field

from .service import MemoryService


@dataclass(slots=True)
class WorkerReport:
    processed: int
    results: list[dict] = field(default_factory=list)


class OutboxWorker:
    """Small worker contract shared by local jobs and future queue runners."""

    def __init__(self, service: MemoryService, *, batch_size: int = 100) -> None:
        self.service = service
        self.batch_size = max(1, min(batch_size, 1000))

    def run_once(self) -> WorkerReport:
        results = self.service.process_pending(self.batch_size)
        return WorkerReport(len(results), results)

    def drain(self, *, max_batches: int = 100) -> WorkerReport:
        all_results: list[dict] = []
        for _ in range(max(1, max_batches)):
            batch = self.run_once()
            all_results.extend(batch.results)
            if batch.processed == 0:
                break
        return WorkerReport(len(all_results), all_results)
