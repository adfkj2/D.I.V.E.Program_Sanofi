from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


EVOLUTION_RELATIONSHIPS = frozenset({
    "unrelated", "duplicate", "reinforcement", "refinement", "correction",
    "temporal_update", "contradiction", "supersession",
})


@dataclass(frozen=True, slots=True)
class EvolutionGoldCase:
    case_id: str
    relationship: str
    language: str
    previous: dict[str, Any]
    candidate: dict[str, Any]
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvolutionGoldSuite:
    schema_version: str
    cases: tuple[EvolutionGoldCase, ...]


def load_internal_evolution_suite(path: str | Path | None = None) -> EvolutionGoldSuite:
    source = Path(path) if path else Path(__file__).resolve().parents[2] / "eval" / "datasets" / "internal_v1" / "evolution.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "dive-internal-evolution-v1":
        raise ValueError("unsupported internal evolution schema")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("cases must be a list")
    cases: list[EvolutionGoldCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        case_id = str(raw.get("case_id", ""))
        relationship = str(raw.get("relationship", ""))
        language = str(raw.get("language", ""))
        sources = raw.get("source_event_ids")
        if not case_id or case_id in seen:
            raise ValueError("case ids must be non-empty and unique")
        if relationship not in EVOLUTION_RELATIONSHIPS:
            raise ValueError(f"unknown relationship: {relationship}")
        if language not in {"zh", "en"}:
            raise ValueError(f"unsupported language: {language}")
        if not isinstance(sources, list) or not sources or any(not isinstance(item, str) for item in sources):
            raise ValueError(f"{case_id}: source_event_ids must be non-empty strings")
        if not isinstance(raw.get("previous"), dict) or not isinstance(raw.get("candidate"), dict):
            raise ValueError(f"{case_id}: previous and candidate must be objects")
        seen.add(case_id)
        cases.append(EvolutionGoldCase(case_id, relationship, language, raw["previous"], raw["candidate"], tuple(sources)))
    return EvolutionGoldSuite(payload["schema_version"], tuple(cases))
