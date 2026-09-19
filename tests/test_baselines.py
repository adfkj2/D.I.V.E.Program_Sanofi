from dive_memory.baselines import full_history, hybrid_memory, structured_memory, summary_vector, vector_rag
from dive_memory.service import MemoryService


def test_baselines_are_comparable():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert full_history(service, "u1", "绿茶").name == "A-full-history"
    assert vector_rag(service, "u1", "绿茶").evidence
    assert summary_vector(service, "u1", "绿茶").name == "C-summary-vector"
    assert structured_memory(service, "u1", "preference: 绿茶").name == "D-structured-memory"
    assert hybrid_memory(service, "u1", "绿茶").evidence
