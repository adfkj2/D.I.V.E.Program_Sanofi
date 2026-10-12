import pytest
import json
import urllib.request
import sys
from types import SimpleNamespace

from dive_memory.embeddings import (
    DeterministicEmbeddingProvider,
    LocalSentenceTransformerEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from dive_memory.service import MemoryService


class TinyEmbedder:
    model = "test-v1"
    dimensions = 2

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "咖啡" in text else [0.0, 1.0]


def test_local_encoder_load_uses_snapshot_path_and_forbids_network(monkeypatch, tmp_path):
    calls = []

    def fake_sentence_transformer(path, **kwargs):
        calls.append((path, kwargs))
        return object()

    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        SimpleNamespace(SentenceTransformer=fake_sentence_transformer))
    provider = LocalSentenceTransformerEmbeddingProvider.load(
        model=str(tmp_path), revision="pinned-revision", dimensions=2,
    )

    assert provider.model == str(tmp_path)
    assert calls == [(str(tmp_path.resolve()), {"device": None, "local_files_only": True})]


def test_local_encoder_missing_cache_fails_before_model_loader(monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(try_to_load_from_cache=lambda *_args, **_kwargs: None))
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        SimpleNamespace(SentenceTransformer=lambda *_args, **_kwargs:
                                        (_ for _ in ()).throw(AssertionError("must not load"))))

    with pytest.raises(RuntimeError, match="no download was attempted"):
        LocalSentenceTransformerEmbeddingProvider.load(
            model="not-cached/example", revision="pinned-revision", dimensions=2,
        )


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
    with pytest.raises(RuntimeError, match="full reindex"):
        switched.store.reindex_vectors(namespace="u1")
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


def test_vector_baseline_abstains_until_model_migration_is_complete(tmp_path):
    from dive_memory.baselines import vector_rag

    db_path = str(tmp_path / "memory.sqlite3")
    first = MemoryService(db_path)
    first.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    first.store.close()
    switched = MemoryService(db_path, embedder=TinyEmbedder())
    assert vector_rag(switched, "u1", "咖啡").evidence == []


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


def test_embedding_fallback_is_normalised(monkeypatch):
    class NonNormalisedFallback:
        model = "fallback"
        dimensions = 2

        def embed(self, text):
            return [3.0, 4.0]

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    provider = OpenAICompatibleEmbeddingProvider(
        "http://embedding.test", "test-model", "test-key", 2, fallback=NonNormalisedFallback(),
    )
    assert provider.embed("咖啡") == [0.6, 0.8]


def test_embedding_fallback_is_explicitly_degraded(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    provider = OpenAICompatibleEmbeddingProvider(
        "http://embedding.test", "test-model", "test-key", 96,
        fallback=DeterministicEmbeddingProvider(),
    )
    result = provider.embed_result("咖啡")
    assert result.degraded
    assert not result.semantic
    assert result.model == "test-model"
    assert result.fallback_model == "deterministic-sha256-v1"
