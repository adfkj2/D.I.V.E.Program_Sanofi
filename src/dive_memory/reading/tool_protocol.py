"""Pure action-shape validation for the opt-in verified reader."""

from __future__ import annotations

from typing import Any


class RecoverableToolError(ValueError):
    """A model may correct this request within the existing call budget."""


class EvidenceSourceError(Exception):
    """A source failed during execution, rather than rejecting model syntax."""


def validate_action(obj: dict[str, Any]) -> dict[str, Any]:
    """Reject extra fields before reporting missing fields; never fill defaults."""
    schemas = {
        "search": {"action", "query"},
        "read": {"action", "memory_id", "start"},
        "calculate": {"action", "operation", "operands", "unit"},
    }
    action = obj.get("action")
    if not isinstance(action, str) or action not in schemas:
        raise ValueError("unknown tool action")
    expected = schemas[action]
    if set(obj) - expected:
        raise ValueError("unexpected tool action fields")
    if missing := expected - set(obj):
        raise RecoverableToolError("missing tool fields: " + ", ".join(sorted(missing)))
    # Use exactly the same query for execution and duplicate detection.
    if action == "search" and isinstance(obj["query"], str):
        return {**obj, "query": obj["query"].strip()}
    return obj
