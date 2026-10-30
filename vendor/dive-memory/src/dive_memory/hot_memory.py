"""Reviewable SQLite hot-profile projection; source events remain authoritative.

The profile is an opt-in navigation aid. It is never injected into Oracle
retrieval or counted as answer evidence. All writes below affect derived tables.
"""
from __future__ import annotations

import json
from hashlib import sha256

from . import temporal
from .entities import is_user_subject
from .ids import new_id
from .models import utc_now
from .resolution import _AUTHORITATIVE_SOURCES
from .token_budget import ApproximateTokenCounter

VERSION = "reviewed-hot-profile-v1"
PREDICATES = ("name", "residence", "primary_tool", "goal", "preference")
TOKEN_BUDGET = 256


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive(service, namespace: str) -> tuple[dict, dict, str]:
    """Bounded values with exact memory/version/event/time provenance."""
    now = utc_now()
    eligible = []
    for memory in service.store.active_memories(namespace):
        data = memory.structured_content
        if (not is_user_subject(data.get("subject"))
                or data.get("predicate") not in PREDICATES
                or not isinstance(data.get("value"), str)
                or len(data["value"]) > 240
                or service._memory_source_type(memory) not in _AUTHORITATIVE_SOURCES
                or not temporal.visible_at(memory.valid_from, memory.valid_to, now)
                or (temporal.parse_instant(memory.observed_at) is not None
                    and temporal.parse_instant(memory.observed_at) > temporal.parse_instant(now))):
            continue
        if not memory.source_event_ids or any(
            service.store.is_tombstoned("event", source)
            or service.get_event(source) is None for source in memory.source_event_ids
        ):
            continue
        eligible.append(memory)
    fields, provenance = {}, {}
    for predicate in PREDICATES:
        matches = [m for m in eligible if m.structured_content["predicate"] == predicate]
        # Ambiguous scalar slots are omitted rather than silently selecting one.
        values = {m.structured_content["value"] for m in matches}
        if len(values) != 1:
            continue
        value = next(iter(values))
        proposed = {**fields, predicate: value}
        if ApproximateTokenCounter().count(_json(proposed)) > TOKEN_BUDGET:
            continue
        fields = proposed
        provenance[predicate] = [{
            "memory_id": m.id, "memory_version": m.version,
            "source_event_ids": sorted(m.source_event_ids),
            "valid_from": m.valid_from, "valid_to": m.valid_to,
            "observed_at": m.observed_at,
        } for m in matches]
    digest = sha256(_json({"fields": fields, "provenance": provenance}).encode()).hexdigest()
    return fields, provenance, digest


def _latest(store, namespace):
    return store.db.execute(
        "SELECT * FROM profile_revisions WHERE namespace=? ORDER BY revision DESC LIMIT 1",
        (namespace,),
    ).fetchone()


def current(service, namespace: str) -> dict:
    row = _latest(service.store, namespace)
    _, _, digest = derive(service, namespace)
    stale = row is not None and row["source_digest"] != digest
    return {
        "fields": json.loads(row["current_json"]) if row and not stale else {},
        "provenance": json.loads(row["provenance_json"]) if row and not stale else {},
        "revision": row["revision"] if row else 0,
        "stale": stale, "generator_version": VERSION,
        "purpose": "navigation_only", "token_budget": TOKEN_BUDGET,
    }


def propose(service, namespace: str) -> dict:
    with service.store.transaction(immediate=True):
        fields, provenance, digest = derive(service, namespace)
        row = _latest(service.store, namespace)
        previous = json.loads(row["current_json"]) if row else {}
        revision = row["revision"] if row else 0
        diff = {key: {"before": previous.get(key), "after": fields.get(key)}
                for key in sorted(set(previous) | set(fields))
                if previous.get(key) != fields.get(key)}
        proposal_id = new_id("profile_proposal")
        service.store.db.execute(
            "INSERT INTO profile_proposals(id,namespace,base_revision,proposed_json,provenance_json,"
            "source_digest,diff_json,generator_version,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, namespace, revision, _json(fields), _json(provenance), digest,
             _json(diff), VERSION, "PENDING", utc_now()),
        )
    return {"proposal_id": proposal_id, "namespace": namespace, "base_revision": revision,
            "fields": fields, "provenance": provenance, "diff": diff, "status": "PENDING"}


def _append(store, namespace, fields, provenance, digest, reason):
    latest = _latest(store, namespace)
    revision = latest["revision"] + 1 if latest else 1
    store.db.execute(
        "INSERT INTO profile_revisions(namespace,revision,current_json,provenance_json,"
        "source_digest,reason,created_at) VALUES (?,?,?,?,?,?,?)",
        (namespace, revision, _json(fields), _json(provenance), digest, reason, utc_now()),
    )
    return revision


def review(service, namespace: str, proposal_id: str, *, approve: bool, reason: str) -> dict:
    if not reason.strip():
        raise ValueError("a review reason is required")
    with service.store.transaction(immediate=True):
        row = service.store.db.execute(
            "SELECT * FROM profile_proposals WHERE namespace=? AND id=?", (namespace, proposal_id),
        ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        if row["status"] != "PENDING":
            raise ValueError("proposal is no longer pending")
        latest = _latest(service.store, namespace)
        revision = latest["revision"] if latest else 0
        fields, provenance, digest = derive(service, namespace)
        if not approve:
            status = "REJECTED"
        elif (revision != row["base_revision"] or digest != row["source_digest"]
              or fields != json.loads(row["proposed_json"])
              or provenance != json.loads(row["provenance_json"])):
            status = "STALE"
        else:
            status = "APPROVED"
            revision = _append(service.store, namespace, fields, provenance, digest,
                               f"approve:{proposal_id}:{reason}")
        service.store.db.execute(
            "UPDATE profile_proposals SET status=?,reviewed_at=?,review_reason=? WHERE id=?",
            (status, utc_now(), reason, proposal_id),
        )
    return {"proposal_id": proposal_id, "status": status, "revision": revision}


def rollback(service, namespace: str, revision: int, *, reason: str) -> dict:
    """Append a previous navigation view only while every cited source is valid."""
    if not reason.strip():
        raise ValueError("a rollback reason is required")
    with service.store.transaction(immediate=True):
        row = service.store.db.execute(
            "SELECT * FROM profile_revisions WHERE namespace=? AND revision=?", (namespace, revision),
        ).fetchone()
        if row is None:
            raise KeyError(revision)
        fields, provenance, digest = derive(service, namespace)
        prior_fields, prior_sources = json.loads(row["current_json"]), json.loads(row["provenance_json"])
        if row["reason"] == "forgotten_sources" or any(
            fields.get(key) != value or provenance.get(key) != prior_sources.get(key)
            for key, value in prior_fields.items()
        ):
            raise ValueError("rollback sources changed, expired or were forgotten")
        new_revision = _append(service.store, namespace, prior_fields, prior_sources, digest,
                               f"rollback:{revision}:{reason}")
    return {"revision": new_revision, "rolled_back_to": revision}


def redact(store, namespace: str) -> None:
    """Forget removes derived content too; retain only revision numbers/timestamps."""
    store.redact_profile_history(namespace)
