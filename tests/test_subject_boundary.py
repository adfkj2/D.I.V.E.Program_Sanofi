"""Gate / entity-extraction responsibility boundary.

The false-memory suite's ``ec-third-party-residence`` case asks two things at
once:

* ``memory_expected: true``  — the fact must be remembered;
* ``user_fact_allowed: false`` — it must not become a *user* fact.

The v1 write gate is asked only whether text is worth storing, so it accepts
the sentence. That is correct. The failure was downstream: the extractor
hard-coded ``subject='user'`` and the profile projection ignored the subject, so
"Alice lives in Shanghai" was projected as the user's own residence.

These tests pin the split: the gate owns *worth*, the extractor owns *whose
fact it is*.
"""
from __future__ import annotations

import pytest

from dive_memory.entities import entity_candidates, is_user_subject
from dive_memory.extraction import extract_candidates
from dive_memory.service import MemoryService


def test_third_party_statement_is_not_projected_onto_the_user_profile():
    service = MemoryService()
    service.ingest("u1", "Alice lives in Shanghai", explicit=True, source_type="user")

    profile = service.profile("u1")
    assert "residence" not in profile
    assert "lives_in" not in profile


def test_third_party_statement_is_still_remembered():
    service = MemoryService()
    result = service.ingest("u1", "Alice lives in Shanghai", explicit=True, source_type="user")

    assert result["memory_ids"]
    memory = service.get_memory(result["memory_ids"][0])
    assert memory.structured_content["subject"] == "alice"
    assert memory.structured_content["value"] == "Shanghai"
    assert memory.status.value == "ACTIVE"


def test_third_party_memory_is_linked_to_the_person_entity():
    service = MemoryService()
    result = service.ingest("u1", "Alice lives in Shanghai", explicit=True, source_type="user")

    names = {
        row["canonical_name"]
        for row in service.store.db.execute(
            "SELECT e.canonical_name FROM memory_entities me JOIN entities e ON e.id=me.entity_id "
            "WHERE me.memory_id=?", (result["memory_ids"][0],),
        )
    }
    assert "alice" in names


def test_third_party_statement_creates_no_user_relation():
    service = MemoryService()
    result = service.ingest("u1", "Alice lives in Shanghai", explicit=True, source_type="user")

    relations = service.store.db.execute(
        "SELECT COUNT(*) AS n FROM relations WHERE namespace=?", ("u1",),
    ).fetchone()
    assert relations["n"] == 0
    assert result["memory_ids"]


def test_user_statement_about_someone_else_does_not_touch_the_users_own_residence():
    """The stricter reading of ``ec-third-party-residence``."""
    service = MemoryService()
    service.ingest("u1", "我住在成都", explicit=True, source_type="user")
    service.ingest(
        "u1", "Alice lives in Shanghai; do not treat this as my residence.",
        explicit=True, source_type="user",
    )

    assert service.profile("u1")["residence"] == "成都"


def test_first_person_residence_still_reaches_the_profile():
    service = MemoryService()
    service.ingest("u1", "我住在成都", explicit=True, source_type="user")

    assert service.profile("u1")["residence"] == "成都"


def test_third_party_facts_are_deduplicated_by_subject():
    service = MemoryService()
    service.ingest("u1", "Alice lives in Shanghai", explicit=True, source_type="user")
    service.ingest(
        "u1", "Alice lives in Beijing", explicit=True, source_type="user",
        observed_at="2026-01-01T00:00:00+00:00",
    )

    subjects = {
        memory.structured_content.get("subject") for memory in service.list_memories("u1")
    }
    assert subjects == {"alice"}


@pytest.mark.parametrize("subject", [None, "user", "User", "self", "me"])
def test_user_subject_recognition(subject):
    assert is_user_subject(subject)


@pytest.mark.parametrize("subject", ["alice", "Bob", "another customer"])
def test_non_user_subject_recognition(subject):
    assert not is_user_subject(subject)


def test_extraction_separates_subject_from_predicate():
    candidates = extract_candidates("Alice lives in Shanghai", explicit=True)
    assert len(candidates) == 1
    structured = candidates[0].structured_content
    assert structured["subject"] == "alice"
    assert structured["predicate"] == "residence"
    assert structured["value"] == "Shanghai"


def test_extraction_keeps_first_person_subject_as_user():
    candidates = extract_candidates("我住在成都", explicit=True)
    structured = candidates[0].structured_content
    assert structured["subject"] == "user"
    assert structured["predicate"] == "residence"


def test_third_party_entity_candidates_include_the_person():
    candidates = entity_candidates("Alice lives in Shanghai", {
        "subject": "alice", "predicate": "residence", "value": "Shanghai",
    })
    assert ("alice", "person") in candidates
    assert ("shanghai", "location") in candidates


def test_first_person_entity_candidates_omit_a_user_entity():
    candidates = entity_candidates("我住在成都", {
        "subject": "user", "predicate": "residence", "value": "成都",
    })
    assert all(name != "user" for name, _ in candidates)
    assert ("成都", "location") in candidates
