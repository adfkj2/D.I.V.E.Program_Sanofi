from __future__ import annotations

import json

from .evaluation import EvalCase, run_cases
from .service import MemoryService


def run_smoke() -> dict:
    service = MemoryService()
    first = service.ingest("demo", "请记住我住在成都", explicit=True, observed_at="2025-01-01T00:00:00+00:00")
    service.ingest("demo", "我住在上海", explicit=True, observed_at="2026-01-01T00:00:00+00:00")
    current = service.retrieve("demo", "上海")
    return run_cases(service, [
        EvalCase("current", "demo", "上海", "上海", [current.items[0].memory.id] if current.items else []),
        EvalCase("historical", "demo", "以前住成都", "成都", first["memory_ids"]),
        EvalCase("abstain", "demo", "火星住哪里", None, [], should_abstain=True),
    ])


if __name__ == "__main__":
    print(json.dumps(run_smoke(), ensure_ascii=False, indent=2))
