import math

import pytest

from dive_memory.candidate import CandidateMemoryV1, CandidateValidationError, Explicitness


def _valid_payload():
    return {
        "schema_version": "candidate-memory-v1",
        "memory_type": "preference",
        "subject": "user",
        "predicate": "preference",
        "value": "  Green   Tea  ",
        "normalized_value": "green tea",
        "valid_from": "2025-01-01T00:00:00+00:00",
        "valid_to": None,
        "confidence": 0.9,
        "importance": 0.8,
        "durability": "long_term",
        "evidence_state": "FACT",
        "explicitness": "EXPLICIT",
        "evidence_spans": ["Green   Tea"],
        "entities": ["Green Tea"],
        "relations": [],
    }


def test_candidate_v1_validates_and_binds_provenance():
    candidate = CandidateMemoryV1.from_provider_payload(
        _valid_payload(), source_text="Please remember that I prefer Green   Tea."
    ).with_sources(["evt-1"])

    assert candidate.normalized_value == "green tea"
    assert candidate.explicitness is Explicitness.EXPLICIT
    assert candidate.source_event_ids == ("evt-1",)
    candidate.validate_for_commit()


@pytest.mark.parametrize("field,value", [
    ("confidence", -0.01), ("confidence", 1.01), ("confidence", math.nan),
    ("importance", -0.01), ("importance", 1.01),
    ("memory_type", "invented"), ("evidence_state", "MAYBE"),
    ("explicitness", "SORT_OF"), ("durability", "forever-ish"),
])
def test_candidate_v1_rejects_invalid_enums_and_bounds(field, value):
    payload = _valid_payload()
    payload[field] = value
    with pytest.raises(CandidateValidationError):
        CandidateMemoryV1.from_provider_payload(payload, source_text="I prefer Green   Tea")


def test_candidate_v1_rejects_invalid_interval_and_unsupported_span():
    payload = _valid_payload()
    payload["valid_to"] = "2024-12-31T00:00:00+00:00"
    with pytest.raises(CandidateValidationError, match="valid_from"):
        CandidateMemoryV1.from_provider_payload(payload, source_text="Green   Tea")

    payload = _valid_payload()
    payload["evidence_spans"] = ["invented evidence"]
    with pytest.raises(CandidateValidationError, match="evidence span"):
        CandidateMemoryV1.from_provider_payload(payload, source_text="Green   Tea")


def test_candidate_v1_requires_source_before_commit():
    candidate = CandidateMemoryV1.from_provider_payload(_valid_payload(), source_text="Green   Tea")
    with pytest.raises(CandidateValidationError, match="source event"):
        candidate.validate_for_commit()
