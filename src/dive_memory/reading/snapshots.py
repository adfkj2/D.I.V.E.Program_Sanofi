"""Immutable identities for admitted evidence, separate from retrieval events.

Only explicitly reserved acquisition annotations are ignored. Domain fields such
as an asserted ``access_count`` retain their meaning; arbitrary lists keep order.
Source-reference and provenance-record lists are sets of complete records, so a
conflicting role or clock is retained rather than overwritten by source ID.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from ..models import RetrievalItem

VERSION = "admitted-evidence-snapshot-v1"
_ACCESS_FIELDS = frozenset(
    {
        "access_count",
        "retrieval_count",
        "last_access",
        "last_accessed_at",
        "last_retrieved_at",
        "retrieved_at",
    }
)
_ROOT_ACCESS_FIELDS = frozenset("_" + field for field in _ACCESS_FIELDS)
_RETRIEVAL_FIELDS = _ACCESS_FIELDS | {
    "score",
    "channels",
    "rank",
    "query",
    "position",
    "elapsed_ms",
}
_RETRIEVAL_NAMESPACES = frozenset(
    {"_retrieval", "_retrieval_metadata", "_reader_acquisition"}
)
_SOURCE_ANNOTATIONS = frozenset(
    {
        "_reader_statement_sources",
        "_reader_sources",
        "_reader_source_stamps",
        "_source_stamps",
    }
)


def _encode(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _semantic_structured(structured: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in structured.items():
        if key in _ROOT_ACCESS_FIELDS:
            continue
        if key in _RETRIEVAL_NAMESPACES and isinstance(value, dict):
            remaining = {k: v for k, v in value.items() if k not in _RETRIEVAL_FIELDS}
            if remaining:
                result[key] = remaining
        elif key == "_meta" and isinstance(value, dict):
            remaining = {k: v for k, v in value.items() if k not in _ACCESS_FIELDS}
            if remaining:
                result[key] = remaining
        elif key in _SOURCE_ANNOTATIONS and isinstance(value, (list, tuple)):
            # Keep every distinct full record; source-ref-only merging would
            # erase a conflict between two statements with different clocks.
            unique = {_encode(record): record for record in value}
            result[key] = [unique[encoded] for encoded in sorted(unique)]
        else:
            result[key] = value
    return result


def acquisition_annotations(item: RetrievalItem) -> dict[str, Any]:
    """Capture only the ignored, reserved acquisition fields, never arbitrary data."""
    result = {}
    for key, value in item.memory.structured_content.items():
        if key in _ROOT_ACCESS_FIELDS:
            result[key] = deepcopy(value)
        elif key in _RETRIEVAL_NAMESPACES and isinstance(value, dict):
            ignored = {k: v for k, v in value.items() if k in _RETRIEVAL_FIELDS}
            if ignored:
                result[key] = deepcopy(ignored)
        elif key == "_meta" and isinstance(value, dict):
            ignored = {k: v for k, v in value.items() if k in _ACCESS_FIELDS}
            if ignored:
                result[key] = deepcopy(ignored)
    return result


@dataclass(frozen=True, slots=True)
class AdmittedSnapshot:
    """A canonical string has no mutable references to caller-owned evidence."""

    canonical_json: str

    @classmethod
    def from_item(cls, item: RetrievalItem) -> AdmittedSnapshot:
        memory = asdict(item.memory)
        memory["structured_content"] = _semantic_structured(
            memory["structured_content"]
        )
        memory["source_event_ids"] = sorted(set(memory["source_event_ids"]))
        return cls(
            _encode(
                {
                    "version": VERSION,
                    "memory": memory,
                    "source_refs": sorted(set(item.source_refs)),
                }
            )
        )

    def fingerprint(self) -> str:
        return sha256(self.canonical_json.encode("utf-8")).hexdigest()
