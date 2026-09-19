from __future__ import annotations

from dataclasses import dataclass

from .ids import cosine, stable_vector
from .models import Memory
from .service import MemoryService


@dataclass(slots=True)
class BaselineResult:
    name: str
    evidence: list[str]
    estimated_tokens: int


def full_history(service: MemoryService, namespace: str, query: str) -> BaselineResult:
    events = list(service.store.events_for_namespace(namespace))
    texts = [event.payload.get("text", "") for event in events]
    joined = "\n".join(texts)
    return BaselineResult("A-full-history", texts, max(1, len(joined) // 4))


def vector_rag(service: MemoryService, namespace: str, query: str, limit: int = 5) -> BaselineResult:
    query_vec = service.store.embedder.embed(query)
    rows = service.store.db.execute(
        "SELECT m.*, v.vector FROM memories m JOIN memory_vectors v ON v.memory_id=m.id "
        "WHERE m.namespace=? AND m.status='ACTIVE'", (namespace,)
    ).fetchall()
    scored = [(cosine(query_vec, __import__("json").loads(row["vector"])), row["content"]) for row in rows]
    scored.sort(reverse=True)
    evidence = [text for score, text in scored[:limit] if score > 0.15]
    return BaselineResult("B-vector-rag", evidence, sum(len(x) for x in evidence) // 4)


def summary_vector(service: MemoryService, namespace: str, query: str, limit: int = 5) -> BaselineResult:
    # A deterministic local summary baseline. A production run can replace the
    # reducer with an LLM while keeping the same token accounting.
    history = full_history(service, namespace, query)
    summary = " ".join(history.evidence)[-2000:]
    return BaselineResult("C-summary-vector", [summary] if summary else [], max(1, len(summary) // 4))


def structured_memory(service: MemoryService, namespace: str, query: str, limit: int = 5) -> BaselineResult:
    terms = {term.lower() for term in query.split()}
    evidence = []
    for memory in service.store.active_memories(namespace):
        haystack = f"{memory.content} {memory.structured_content}".lower()
        if terms & set(haystack.split()):
            evidence.append(memory.content)
    return BaselineResult("D-structured-memory", evidence[:limit], sum(len(x) for x in evidence[:limit]) // 4)


def hybrid_memory(service: MemoryService, namespace: str, query: str, limit: int = 5) -> BaselineResult:
    result = service.retrieve(namespace, query, limit=limit)
    evidence = [item.memory.content for item in result.items]
    return BaselineResult("E-hybrid-memory", evidence, sum(len(x) for x in evidence) // 4)
