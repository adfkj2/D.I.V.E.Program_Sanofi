from dive_memory.baselines import full_history, hybrid_memory, vector_rag
from dive_memory.service import MemoryService


def test_baselines_are_comparable():
    service = MemoryService()
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert full_history(service, "u1", "绿茶").name == "A-full-history"
    assert vector_rag(service, "u1", "绿茶").evidence
    assert hybrid_memory(service, "u1", "绿茶").evidence
