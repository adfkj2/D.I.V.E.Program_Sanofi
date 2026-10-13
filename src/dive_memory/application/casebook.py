"""Load curated repository history through ordinary project-record use cases.

The distributable resource contains exact source excerpts and their frozen
provenance. Runtime loading never reads repository documentation or evaluation
outputs and contains no question-to-answer lookup table.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from .records import ProjectRecord
from .runtime import MemoryApplication

RECORD_FIELDS = (
    "key",
    "value",
    "category",
    "disposition",
    "reason",
    "session",
    "source_title",
    "source_path",
    "source_excerpt",
    "source_sha256",
)


def read_casebook() -> dict[str, Any]:
    """Return the installed, dated manual mapping with frozen source excerpts."""
    payload = json.loads(
        files("dive_memory")
        .joinpath("data/project-casebook.json")
        .read_text(encoding="utf-8")
    )
    if payload.get("schema") != "dive-project-casebook-v1":
        raise ValueError("unsupported project casebook schema")
    return payload


def load_casebook(app: MemoryApplication, namespace: str) -> dict[str, Any]:
    """Persist an explicitly curated historical case; repeat loads are idempotent."""
    casebook = read_casebook()
    prepared: list[tuple[dict, ProjectRecord]] = []
    records_by_id: dict[str, ProjectRecord] = {}
    for row in casebook["records"]:
        record = ProjectRecord(**{field: row[field] for field in RECORD_FIELDS})
        identifier = row["id"]
        if identifier in records_by_id:
            raise ValueError("duplicate project casebook record id")
        if row.get("replaces_id"):
            previous = records_by_id.get(row["replaces_id"])
            if (
                previous is None
                or previous.key != row.get("replaces_key")
                or previous.key != record.key
                or previous.category != record.category
            ):
                raise ValueError(
                    "casebook replacement must name an earlier matching record"
                )
        records_by_id[identifier] = record
        prepared.append((row, record))

    events = []
    memory_ids: dict[str, str] = {}
    for row, record in prepared:
        replacement = row.get("replaces_id")
        if replacement and replacement not in memory_ids:
            raise ValueError(
                "A historical source was erased from this project. "
                "Load the casebook into a new project to preserve that erasure."
            )
        result = app.record(
            namespace,
            record,
            idempotency_key=f"{casebook['id']}:{row['id']}",
            observed_at=row["observed_at"],
            replaces=memory_ids[replacement] if replacement else None,
        )
        if result["memory_ids"]:
            memory_ids[row["id"]] = result["memory_ids"][0]
        events.append(result)
    return {
        "namespace": namespace,
        "title": casebook["title"],
        "casebook_id": casebook["id"],
        "notice": casebook["notice"],
        "time_notice": casebook["time_notice"],
        "as_of_date": casebook["as_of_date"],
        "frozen_at": casebook["frozen_at"],
        "synthetic": False,
        "curated": True,
        "events": events,
    }
