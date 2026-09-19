from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .gate import GateDecision, decide


@dataclass(slots=True)
class Candidate:
    content: str
    kind: str
    evidence_state: str
    structured_content: dict[str, Any]
    decision: GateDecision
    valid_from: str | None = None
    valid_to: str | None = None


def _fact_from_text(text: str) -> tuple[str, dict[str, Any]]:
    patterns = [
        (r"我住在\s*([^。,.，]+)", "residence"),
        (r"我(?:现在)?主要使用\s*([^。,.，]+)", "primary_tool"),
        (r"我喜欢\s*([^。,.，]+)", "preference"),
        (r"我偏好\s*([^。,.，]+)", "preference"),
        (r"我的目标是\s*([^。,.，]+)", "goal"),
    ]
    for pattern, kind in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1).strip()
            return f"{kind}: {value}", {"predicate": kind, "value": value}
    return text.strip(), {"predicate": "statement", "value": text.strip()}


def _explicit_date(text: str) -> str | None:
    match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?", text)
    if not match:
        return None
    year, month, day = (int(value) for value in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}T00:00:00+00:00"


def extract_candidates(text: str, *, explicit: bool = False, observed_at: str | None = None) -> list[Candidate]:
    decision = decide(text, explicit=explicit)
    if not decision.accepted:
        return []
    content, structured = _fact_from_text(text)
    return [Candidate(content, "semantic_fact", "FACT", structured, decision, _explicit_date(text) or observed_at)]
