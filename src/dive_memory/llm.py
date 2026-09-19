from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .extraction import Candidate, extract_candidates
from .gate import decide
from .models import ALLOWED_MEMORY_KINDS


class ExtractionProvider(Protocol):
    def extract(self, text: str, *, observed_at: str | None = None, explicit: bool = False) -> list[Candidate]: ...


class HeuristicExtractionProvider:
    def extract(self, text: str, *, observed_at: str | None = None, explicit: bool = False) -> list[Candidate]:
        return extract_candidates(text, explicit=explicit, observed_at=observed_at)


@dataclass(slots=True)
class OpenAICompatibleExtractionProvider:
    endpoint: str
    model: str
    api_key: str
    timeout_seconds: float = 30.0

    def extract(self, text: str, *, observed_at: str | None = None, explicit: bool = False) -> list[Candidate]:
        prompt = (
            "Extract only durable user memories from the text. Return JSON array with objects "
            "content, kind, evidence_state, predicate, value, importance, confidence, durability. "
            "Return [] for ephemeral or unsupported content.\nText: " + text
        )
        body = json.dumps({"model": self.model, "temperature": 0, "messages": [
            {"role": "system", "content": "You are a conservative memory extraction service."},
            {"role": "user", "content": prompt},
        ]}).encode()
        request = urllib.request.Request(self.endpoint, data=body, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw = payload["choices"][0]["message"]["content"]
            values = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            # A provider outage must preserve the event and fall back to the
            # deterministic extractor rather than inventing a memory.
            return extract_candidates(text, explicit=explicit, observed_at=observed_at)
        candidates: list[Candidate] = []
        for value in values if isinstance(values, list) else []:
            if not isinstance(value, dict) or not value.get("content"):
                continue
            evidence_state = str(value.get("evidence_state", "FACT")).upper()
            if evidence_state not in {"FACT", "OBSERVATION", "INFERENCE"}:
                continue
            gate = decide(value["content"], explicit=explicit)
            if not gate.accepted:
                continue
            kind = str(value.get("kind", "semantic_fact"))
            if kind not in ALLOWED_MEMORY_KINDS:
                continue
            candidates.append(Candidate(
                value["content"], kind,
                evidence_state,
                {"predicate": value.get("predicate", "statement"), "value": value.get("value", value["content"])},
                gate, observed_at,
            ))
        return candidates
