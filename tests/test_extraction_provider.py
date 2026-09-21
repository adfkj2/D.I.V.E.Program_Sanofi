import json
import urllib.request

from dive_memory.llm import ExtractionOutcome, ExtractionOutcomeCode, OpenAICompatibleExtractionProvider
from dive_memory.service import MemoryService


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _provider():
    return OpenAICompatibleExtractionProvider("http://extract.test", "test-model", "secret")


def test_provider_reports_committed_schema_valid_candidates(monkeypatch):
    candidate = {
        "schema_version": "candidate-memory-v1",
        "memory_type": "preference",
        "subject": "user",
        "predicate": "preference",
        "value": "green tea",
        "normalized_value": "green tea",
        "valid_from": None,
        "valid_to": None,
        "confidence": 0.9,
        "importance": 0.8,
        "durability": "long_term",
        "evidence_state": "FACT",
        "explicitness": "EXPLICIT",
        "evidence_spans": ["green tea"],
        "entities": ["green tea"],
        "relations": [],
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _Response({
        "choices": [{"message": {"content": json.dumps([candidate])}}]
    }))

    outcome = _provider().extract_outcome("Remember I prefer green tea", explicit=True)

    assert outcome.code is ExtractionOutcomeCode.COMMITTED
    assert len(outcome.candidates) == 1
    assert outcome.schema_version == "candidate-memory-v1"
    assert not outcome.fallback_used


def test_provider_distinguishes_empty_malformed_and_fallback(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _Response({
        "choices": [{"message": {"content": "[]"}}]
    }))
    assert _provider().extract_outcome("hello").code is ExtractionOutcomeCode.EMPTY

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _Response({
        "choices": [{"message": {"content": "not-json"}}]
    }))
    malformed = _provider().extract_outcome("hello")
    assert malformed.code is ExtractionOutcomeCode.MALFORMED
    assert not malformed.candidates

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    fallback = _provider().extract_outcome("请记住我喜欢绿茶", explicit=True)
    assert fallback.code is ExtractionOutcomeCode.FALLBACK_USED
    assert fallback.fallback_used
    assert fallback.candidates


def test_provider_rejects_schema_valid_but_unsupported_evidence(monkeypatch):
    candidate = {
        "schema_version": "candidate-memory-v1", "memory_type": "semantic_fact",
        "subject": "user", "predicate": "residence", "value": "Paris",
        "normalized_value": "paris", "valid_from": None, "valid_to": None,
        "confidence": 0.9, "importance": 0.8, "durability": "long_term",
        "evidence_state": "FACT", "explicitness": "INFERRED",
        "evidence_spans": ["Paris"], "entities": ["Paris"], "relations": [],
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _Response({
        "choices": [{"message": {"content": json.dumps([candidate])}}]
    }))
    outcome = _provider().extract_outcome("I live in Berlin")
    assert outcome.code is ExtractionOutcomeCode.MALFORMED
    assert outcome.candidates == ()


def test_service_persists_provider_outcome_and_versioned_gate_features():
    class MalformedExtractor:
        def extract_outcome(self, text, *, observed_at=None, explicit=False):
            return ExtractionOutcome(
                ExtractionOutcomeCode.MALFORMED,
                reason="invalid candidate payload",
                provider="test-provider",
                model="test-model",
                prompt_version="prompt-v7",
                schema_version="candidate-memory-v1",
            )

    service = MemoryService(extractor=MalformedExtractor())
    result = service.ingest("u1", "remember something", explicit=True)
    row = service.store.db.execute(
        "SELECT accepted,outcome_code,reason,features_json,policy_version,prompt_version,model_version,schema_version "
        "FROM write_decisions WHERE event_id=?", (result["event_id"],),
    ).fetchone()

    assert row["accepted"] == 0
    assert row["outcome_code"] == "MALFORMED"
    assert row["reason"] == "invalid candidate payload"
    assert json.loads(row["features_json"]) == {}
    assert row["policy_version"] == "utility-baseline-v1"
    assert (row["prompt_version"], row["model_version"], row["schema_version"]) == (
        "prompt-v7", "test-model", "candidate-memory-v1",
    )


def test_service_persists_accepted_gate_features():
    service = MemoryService()
    result = service.ingest("u1", "请记住我喜欢绿茶", explicit=True)
    row = service.store.db.execute(
        "SELECT outcome_code,features_json,policy_version FROM write_decisions WHERE event_id=?",
        (result["event_id"],),
    ).fetchone()

    assert row["outcome_code"] == "COMMITTED"
    assert json.loads(row["features_json"])["explicit_remember_request"] == 1.0
    assert row["policy_version"] == "utility-baseline-v1"
