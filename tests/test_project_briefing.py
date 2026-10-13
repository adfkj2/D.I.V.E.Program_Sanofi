"""Product contracts for deterministic, source-backed project continuation."""

import json

import pytest
from fastapi.testclient import TestClient

from dive_memory.application.records import PREFIX, ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.web import create_workbench


@pytest.fixture
def app(tmp_path):
    runtime = MemoryApplication(str(tmp_path / "project.db"))
    yield runtime
    runtime.close()


def add(
    app,
    key,
    value,
    *,
    day=1,
    category="constraint",
    disposition="confirmed",
    reason="",
    replaces=None,
    project="p",
    **source,
):
    result = app.record(
        project,
        ProjectRecord(key, value, category, disposition, reason, **source),
        idempotency_key=f"{key}-{value}-{day}-{disposition}",
        observed_at=f"2026-01-{day:02d}T12:00:00+00:00",
        replaces=replaces,
    )
    return result["memory_ids"][0]


def section_items(brief, disposition=None):
    items = [item for section in brief["sections"] for item in section["items"]]
    if disposition is None:
        return items
    return [
        item
        for item in items
        if item["structured_content"]["disposition"] == disposition
    ]


def assert_citations_owned_and_verbatim(app, result, project="p"):
    assert result["citations"]
    for citation in result["citations"]:
        source = app.source(project, citation["memory_id"])
        assert citation["quote"]
        assert citation["quote"] in source["memory"]["content"]


def test_conflicts_are_explicit_and_never_enter_confirmed_sections(app):
    first = add(app, "budget", "20")
    second = add(app, "budget", "40", day=2)
    safe = add(app, "latency", "2 seconds")
    proposed = add(app, "baseline", "C", category="decision", disposition="proposed")
    rejected = add(app, "alternative", "B", category="decision", disposition="rejected")

    brief = app.brief("p")
    assert brief["mode"] == "local_records"
    assert {item["id"] for item in section_items(brief, "confirmed")} == {safe}
    assert {item["id"] for item in section_items(brief, "proposed")} == {proposed}
    assert {item["id"] for item in section_items(brief, "rejected")} == {rejected}
    conflict = next(group for group in brief["conflicts"] if group["key"] == "budget")
    assert {item["id"] for item in conflict["items"]} == {first, second}
    result = app.consult("p", "budget")
    assert result["status"] == "ABSTAIN"
    assert "conflict" in result["reason"].lower()
    assert {c["memory_id"] for c in result["citations"]} == {first, second}
    assert_citations_owned_and_verbatim(app, result)


def test_a_proposal_does_not_replace_or_conflict_with_a_confirmed_value(app):
    confirmed = add(app, "baseline", "A", category="decision")
    proposal = add(
        app, "baseline", "C", day=2, category="decision", disposition="proposed"
    )
    brief = app.brief("p")
    assert brief["conflicts"] == []
    assert {item["id"] for item in section_items(brief, "confirmed")} == {confirmed}
    assert {item["id"] for item in section_items(brief, "proposed")} == {proposal}


@pytest.mark.parametrize(
    "disposition,label", [("proposed", "提议"), ("rejected", "未采用")]
)
def test_exact_key_returns_record_disposition_without_promoting_it(
    app, disposition, label
):
    record_id = add(
        app,
        "candidate",
        "C",
        category="decision",
        disposition=disposition,
        reason="Needs an independent experiment",
    )
    result = app.consult("p", "candidate")
    assert result["status"] == "ANSWER"
    assert label in result["answer"]
    assert result["mode"] == "local_records"
    assert {c["memory_id"] for c in result["citations"]} == {record_id}
    assert_citations_owned_and_verbatim(app, result)


def test_history_and_since_keep_intermediate_updates_and_exclude_later_events(app):
    original = add(app, "budget", "20")
    middle = add(app, "budget", "30", day=2, replaces=original)
    newest = add(app, "budget", "40", day=3, replaces=middle)
    historical = app.brief("p", as_of="2026-01-02")
    assert {item["id"] for item in section_items(historical)} == {middle}
    assert newest not in json.dumps(historical)

    changes = app.brief("p", since="2026-01-01", as_of="2026-01-03")["changes"]
    assert len(changes) == 2
    assert {change["after"]["id"] for change in changes} == {middle, newest}
    transitions = {(c["before"]["id"], c["after"]["id"]) for c in changes}
    assert transitions == {(original, middle), (middle, newest)}
    assert all(c["kind"] == "updated" for c in changes)
    assert app.consult("p", "budget", as_of="2026-01-01")["status"] == "ANSWER"


def test_since_boundary_is_exclusive_and_requires_a_valid_interval(app):
    moment = "2026-01-01T12:00:00+00:00"
    add(app, "budget", "20")
    assert app.brief("p", since=moment, as_of="2026-01-02")["changes"] == []
    with pytest.raises(ValueError):
        app.brief("p", since="not-a-date")
    with pytest.raises(ValueError):
        app.brief("p", since="2026-01-03", as_of="2026-01-02")
    result = app.consult("p", "上次以来变化")
    assert result["status"] == "ABSTAIN"
    assert not result["citations"]


def test_local_consult_never_calls_configured_reader_and_preserves_unknowns(app):
    class ForbiddenReader:
        def generate(self, _request):
            raise AssertionError("The local record path must not call an LLM")

    app.reader = ForbiddenReader()
    add(app, "budget", "20")
    assert app.brief("p")["mode"] == "local_records"
    result = app.consult("p", "当前约束")
    assert result["status"] == "ANSWER"
    assert_citations_owned_and_verbatim(app, result)
    unsupported = app.consult("p", "Predict next year's benchmark score")
    assert unsupported["status"] == "UNSUPPORTED"
    assert not unsupported["citations"]
    unknown = app.consult("p", "查询：unknown-key")
    assert unknown["status"] in {"ABSTAIN", "UNSUPPORTED"}
    assert not unknown["citations"]


def test_project_isolation_and_erasure_cover_brief_answers_and_handoff(app):
    record_id = add(app, "private budget", "SECRET-20")
    assert section_items(app.brief("other")) == []
    assert not app.consult("other", "private budget")["citations"]
    app.forget("p", record_id)
    app.service.rebuild_projections("p")
    brief = app.brief("p")
    assert section_items(brief) == []
    assert "SECRET-20" not in json.dumps(brief)
    assert not app.consult("p", "private budget")["citations"]


def test_heuristic_text_is_not_silently_promoted_to_confirmed_project_record(app):
    app.ingest("p", "I live in Paris.", idempotency_key="text", explicit=True)
    assert app.snapshot("p")["memories"]
    assert section_items(app.brief("p")) == []
    assert not app.consult("p", "当前约束")["citations"]


def test_sources_survive_record_brief_and_handoff_without_hash_verification_claim(app):
    fields = dict(
        source_title="Experiment notes",
        source_path="docs/experiment.md",
        source_excerpt="The budget was approved at 20 units.",
        source_sha256="a" * 64,
    )
    record_id = add(app, "budget", "20", **fields)
    brief = app.brief("p")
    item = section_items(brief)[0]
    for key, value in fields.items():
        assert item["structured_content"][key] == value
        assert app.source("p", record_id)["memory"]["structured_content"][key] == value
    assert record_id in brief["markdown"]
    assert "docs/experiment.md" in brief["markdown"]
    assert "已整理记录" in brief["markdown"]


def test_empty_source_fields_preserve_original_event_encoding():
    legacy_fields = dict(
        key="budget",
        value="20",
        category="note",
        disposition="confirmed",
        reason="",
        session="default",
    )
    assert ProjectRecord(**legacy_fields).encode() == PREFIX + json.dumps(
        legacy_fields, ensure_ascii=False, sort_keys=True
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_title", "x" * 301),
        ("source_path", "x" * 1001),
        ("source_excerpt", "x" * 12001),
        ("source_sha256", "z" * 64),
        ("source_sha256", "a" * 63),
        ("source_title", None),
    ],
)
def test_source_metadata_has_strict_size_and_digest_boundaries(field, value):
    with pytest.raises(ValueError):
        ProjectRecord("budget", "20", **{field: value})


def test_api_exposes_source_fields_and_local_modes_without_a_model_key(tmp_path):
    with TestClient(
        create_workbench(MemoryApplication(str(tmp_path / "api.db")))
    ) as client:
        created = client.post(
            "/api/records",
            json={
                "project": "p",
                "key": "budget",
                "value": "20",
                "category": "constraint",
                "idempotency_key": "api-record",
                "observed_at": "2026-01-01",
                "source_title": "Local notes",
                "source_path": "notes.md",
                "source_excerpt": "Budget is 20",
                "source_sha256": "b" * 64,
            },
        )
        assert created.status_code == 200
        brief = client.get("/api/brief", params={"project": "p"})
        assert brief.status_code == 200
        assert brief.json()["mode"] == "local_records"
        result = client.post(
            "/api/consult", json={"project": "p", "question": "budget"}
        )
        assert result.status_code == 200
        assert result.json()["status"] == "ANSWER"
        assert result.json()["mode"] == "local_records"
        assert (
            client.post(
                "/api/consult", json={"project": "p", "question": ""}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/brief", params={"project": "p", "since": "bad"}
            ).status_code
            == 400
        )
