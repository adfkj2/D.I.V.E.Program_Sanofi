import pytest

from dive_memory.embedding_service import EmbeddingLifecycle, EmbeddingLifecycleError
from dive_memory.embeddings import (
    DeterministicEmbeddingProvider,
    LocalSentenceTransformerEmbeddingProvider,
    embed_with_metadata,
)
from dive_memory.service import MemoryService


class TinySemantic:
    semantic_similarity = True

    def __init__(self, model, revision, axis):
        self.model = model
        self.revision = revision
        self.dimensions = 2
        self.provider_name = "tiny-local"
        self.axis = axis

    def embed(self, text):
        return [1.0, 0.0] if self.axis == 0 else [0.0, 1.0]


def test_embedding_result_envelope_marks_provider_identity_and_semantics():
    result = embed_with_metadata(DeterministicEmbeddingProvider(), "tea")
    assert result.model == "deterministic-sha256-v1"
    assert result.revision == "v1"
    assert result.dimensions == 96
    assert not result.semantic
    assert not result.degraded


def test_local_sentence_transformer_adapter_is_lazy_and_normalizes_vectors():
    class Encoder:
        def encode(self, texts, **kwargs):
            assert texts == ["tea"]
            return [[3.0, 4.0]]

    provider = LocalSentenceTransformerEmbeddingProvider(
        encoder=Encoder(), model="BAAI/bge-m3", revision="pinned-sha", dimensions=2,
    )
    result = provider.embed_result("tea")
    assert result.vector == pytest.approx((0.6, 0.8))
    assert result.provider == "sentence-transformers"
    assert result.revision == "pinned-sha"
    assert result.semantic


def test_generation_backfill_cutover_and_rollback_never_mix_vectors():
    old = TinySemantic("old-model", "r1", 0)
    new = TinySemantic("new-model", "r2", 1)
    service = MemoryService(embedder=old)
    service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    lifecycle = EmbeddingLifecycle(service.store)

    new_generation = lifecycle.prepare(new)
    assert lifecycle.backfill(new_generation) == 1
    lifecycle.activate(new_generation)

    metadata = service.store.db.execute(
        "SELECT model,active_generation FROM vector_metadata WHERE id=1"
    ).fetchone()
    assert metadata["model"] == "new-model"
    assert metadata["active_generation"] == new_generation
    assert service.store.embedder is new
    assert {row[0] for row in service.store.db.execute(
        "SELECT DISTINCT generation_id FROM memory_vector_state"
    )} == {new_generation}

    service.ingest("u1", "请记住我喜欢咖啡", explicit=True)
    assert {row[0] for row in service.store.db.execute(
        "SELECT DISTINCT generation_id FROM memory_vector_state"
    )} == {new_generation}

    lifecycle.rollback()
    assert service.store.embedder is old
    assert service.store.db.execute(
        "SELECT model FROM vector_metadata WHERE id=1"
    ).fetchone()[0] == "old-model"


def test_cutover_fails_if_new_writes_are_not_backfilled_or_vectors_degrade():
    service = MemoryService(embedder=TinySemantic("old", "r1", 0))
    service.ingest("u1", "请记住我喜欢茶", explicit=True)
    lifecycle = EmbeddingLifecycle(service.store)
    generation = lifecycle.prepare(TinySemantic("new", "r2", 1))
    lifecycle.backfill(generation)
    service.ingest("u1", "请记住我喜欢咖啡", explicit=True)

    with pytest.raises(EmbeddingLifecycleError, match="backfill incomplete"):
        lifecycle.activate(generation)

    lifecycle.backfill(generation)
    lifecycle.activate(generation)


def test_production_readiness_rejects_deterministic_provider():
    lifecycle = EmbeddingLifecycle(MemoryService().store)
    with pytest.raises(EmbeddingLifecycleError, match="semantic"):
        lifecycle.assert_production_ready()
