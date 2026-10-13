"""Explicit, user-confirmed project records; not inferred benchmark answers.

Natural text continues through the existing extraction provider. Structured
records are a separate input mode, so their results cannot be presented as
automatic natural-language extraction quality.
"""

import json
import re
from dataclasses import asdict, dataclass

from ..extraction import Candidate
from ..gate import GateAction, GateDecision
from ..llm import ExtractionOutcome, ExtractionOutcomeCode, HeuristicExtractionProvider

PREFIX = "DIVE_PROJECT_RECORD_V1\n"
CATEGORIES = frozenset({"constraint", "decision", "result", "preference", "note"})
DISPOSITIONS = frozenset({"confirmed", "proposed", "rejected"})


@dataclass(frozen=True)
class ProjectRecord:
    key: str
    value: str
    category: str = "note"
    disposition: str = "confirmed"
    reason: str = ""
    session: str = "default"
    source_title: str = ""
    source_path: str = ""
    source_excerpt: str = ""
    source_sha256: str = ""

    def __post_init__(self):
        for name, maximum in (
            ("key", 120),
            ("value", 6000),
            ("reason", 2000),
            ("session", 120),
            ("source_title", 300),
            ("source_path", 1000),
            ("source_excerpt", 12000),
            ("source_sha256", 64),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"{name} must be text of at most {maximum} characters")
            if name in {"key", "value", "session"} and not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.category not in CATEGORIES or self.disposition not in DISPOSITIONS:
            raise ValueError("unknown category or disposition")
        if self.source_sha256 and not re.fullmatch(
            r"[a-fA-F0-9]{64}", self.source_sha256
        ):
            raise ValueError("source_sha256 must be a 64-character hexadecimal digest")

    def encode(self) -> str:
        # Preserve the original bytes/idempotency contract when optional source
        # metadata is absent. These are user-supplied references, not verified files.
        values = {
            key: value
            for key, value in asdict(self).items()
            if not key.startswith("source_") or value
        }
        return PREFIX + json.dumps(values, ensure_ascii=False, sort_keys=True)


class ProjectExtractionProvider:
    supports_source_type = True

    def __init__(self, fallback=None):
        self.fallback = fallback or HeuristicExtractionProvider()

    def extract_outcome(
        self, text, *, observed_at=None, explicit=False, source_type=None
    ):
        if not text.startswith(PREFIX):
            return self.fallback.extract_outcome(
                text,
                observed_at=observed_at,
                explicit=explicit,
                source_type=source_type,
            )
        if source_type != "user" or not explicit:
            return ExtractionOutcome(
                ExtractionOutcomeCode.POLICY_SKIP,
                (),
                "structured records require explicit user confirmation",
                "project-record",
                "none",
            )
        record = ProjectRecord(**json.loads(text[len(PREFIX) :]))
        content = (
            f"{record.key}: {record.value}\n[{record.disposition}; {record.category}]"
        )
        if record.reason:
            content += "\nReason: " + record.reason
        decision = GateDecision(
            True,
            0.8,
            1.0,
            0.8,
            "long_term",
            "explicit user-confirmed record; disposition preserved",
            GateAction.WRITE,
            "EXPLICIT_PROJECT_RECORD",
            {},
            "project-record-v1",
        )
        candidate = Candidate(
            content,
            "preference" if record.category == "preference" else "semantic_fact",
            "OBSERVATION",
            {
                **asdict(record),
                "subject": record.key.casefold().strip(),
                "predicate": "project_record",
                "schema": "project-record-v1",
                "role": "user",
                "normalized_value": record.value + "|" + record.disposition,
            },
            decision,
            valid_from=observed_at,
        )
        return ExtractionOutcome(
            ExtractionOutcomeCode.COMMITTED,
            (candidate,),
            "explicit structured record",
            "project-record",
            "none",
            "project-record-v1",
        )

    def extract(self, text, **kwargs):
        return list(self.extract_outcome(text, **kwargs).candidates)
