import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audit_tools"))
from audit_adjudicated_result import audit_citations, audit_submission


def trace():
    outcome = {"status": "ANSWER", "citations": [
        {"memory_id": "m2", "source_refs": ["s2"], "quote": "blue"}]}
    return {"hypothesis": "blue", "reader_context_text":
        "[0] id=m1; source=s1\n    red\n[1] id=m2; source=s2\n    blue",
        "evidence": {"views": [
            {"index": 0, "memory_id": "m1", "source_refs": ["s1"]},
            {"index": 1, "memory_id": "m2", "source_refs": ["s2"]}]},
        "reader": {**outcome, "detail": json.dumps({"stages": [outcome]})}}


def test_citation_must_match_its_own_passage_and_provenance():
    t = trace()
    assert audit_citations(t) == 2
    t["reader"]["citations"][0]["quote"] = "red"
    with pytest.raises(ValueError, match="cited visible passage"):
        audit_citations(t)
    t = trace()
    t["reader"]["citations"][0]["source_refs"] = ["s1"]
    with pytest.raises(ValueError, match="provenance"):
        audit_citations(t)


def test_duplicate_missing_and_changed_submission_answers_are_rejected():
    row = {"question_id": "q", "hypothesis": "blue"}
    audit_submission([row], {"q": trace()})
    for rows in ([row, row], [], [{**row, "hypothesis": "red"}]):
        with pytest.raises(ValueError):
            audit_submission(rows, {"q": trace()})
