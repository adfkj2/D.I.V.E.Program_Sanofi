import pytest
import json
import urllib.request

from dive_memory.embeddings import DeterministicEmbeddingProvider, OpenAICompatibleEmbeddingProvider
from dive_memory.service import MemoryService


class TinyEmbedder:
    model = "test-v1"
    dimensions = 2

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "咖啡" in text else [0.0, 1.0]


def test_custom_embedding_provider_is_used_for_write_and_query():
    service = MemoryService(embedder=TinyEmbedder())
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    retrieved = service.retrieve("u1", "咖啡")
    assert retrieved.items[0].memory.id == result["memory_ids"][0]
    metadata = service.store.db.execute("SELECT model, dimensions FROM vector_metadata WHERE id=1").fetchone()
    assert (metadata["model"], metadata["dimensions"]) == ("test-v1", 2)


def test_reindex_allows_switching_embedding_models(tmp_path):
    db_path = str(tmp_path / "memory.sqlite3")
    first = MemoryService(db_path)
    first.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    first.store.close()

    switched = MemoryService(db_path, embedder=TinyEmbedder())
    assert switched.retrieve("u1", "咖啡").items
    with pytest.raises(RuntimeError):
        switched.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert switched.store.reindex_vectors() == 1
    assert switched.process_pending()[0]["memory_ids"]
    result = switched.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    assert result["memory_ids"]


def test_reindex_does_not_restore_deleted_vectors():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    service.forget(result["memory_ids"][0])
    assert service.store.reindex_vectors() == 0
    row = service.store.db.execute("SELECT 1 FROM memory_vectors WHERE memory_id=?", (result["memory_ids"][0],)).fetchone()
    assert row is None


def test_openai_compatible_embedding_falls_back_without_inventing_vector():
    provider = OpenAICompatibleEmbeddingProvider(
        "http://127.0.0.1:1/embeddings", "test-model", "test-key", 96,
        timeout_seconds=0.01, fallback=DeterministicEmbeddingProvider(),
    )
    vector = provider.embed("咖啡")
    assert len(vector) == 96
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-6


def test_openai_compatible_embedding_parses_standard_response(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"data": [{"embedding": [3.0, 4.0]}]}).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response())
    provider = OpenAICompatibleEmbeddingProvider("http://embedding.test", "test-model", "test-key", 2)
    assert provider.embed("咖啡") == [0.6, 0.8]
