"""Selective episodic retention, separate from promotion to stable user facts.

Query independent, verbatim, user-only and bounded. This is not an archive
search bypass: only admitted observations enter the ordinary memory store.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .extraction import Candidate
from .gate import GateAction, GateDecision, _sensitive

VERSION = "selective-episodic-v1"
PREDICATE = "episodic_statement"
_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|[\r\n]+")
_PERSON = re.compile(r"\b(?:I|I've|I'm|my|we|we've|our)\b|我|我们", re.I)
_EVENT = re.compile(
    r"\b(?:bought|purchased|paid|spent|received|got|went|visited|attended|joined|"
    r"started|finished|completed|booked|ordered|travelled|traveled|moved|worked|"
    r"volunteered|ran|walked|read|watched|played|made|cooked|planted|harvested|"
    r"collected|caught|adopted|learned|learnt|met|earned|saved|won|lost|"
    r"have|has|had|own|owns|been)\b|买了|去了|参加|开始|完成|预订|收到了|花了|拥有",
    re.I,
)
_DETAIL = re.compile(
    r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|dozen|"
    r"yesterday|today|last|ago|week|month|year|recently)\b|昨天|上周|上月|去年|最近",
    re.I,
)
_UNASSERTED = re.compile(
    r"\b(?:if|suppose|imagine|hypothetically|would|could|might|wish|hope|"
    r"planning|plan to|want to|intend|example|fiction|pretend|roleplay)\b|"
    r"假如|如果|假设|想象|打算|计划|希望|虚构", re.I,
)


@dataclass(frozen=True)
class EpisodicRetentionPolicy:
    max_items: int = 4
    max_sentence_chars: int = 1000
    max_turn_chars: int = 8000

    def __post_init__(self):
        if min(self.max_items, self.max_sentence_chars, self.max_turn_chars) <= 0:
            raise ValueError("episodic retention limits must be positive")

    @staticmethod
    def _sentences(text):
        start = 0
        for boundary in _BOUNDARY.finditer(text):
            if re.search(r"\b(?:Dr|Mr|Mrs|Ms|Prof)\.$", text[start:boundary.start()]):
                continue
            raw = text[start:boundary.start()]
            yield start + len(raw) - len(raw.lstrip()), raw.strip()
            start = boundary.end()
        raw = text[start:]
        yield start + len(raw) - len(raw.lstrip()), raw.strip()

    def candidates(self, text: str, *, source_type: str | None,
                   observed_at: str | None, existing: list[Candidate]) -> list[Candidate]:
        if source_type != "user" or _sensitive(text) or len(text) > self.max_turn_chars:
            return []
        result: list[Candidate] = []
        seen: set[str] = set()
        for start, sentence in self._sentences(text):
            if (len(sentence) < 15 or len(sentence) > self.max_sentence_chars
                    or sentence.endswith(("?", "？")) or _UNASSERTED.search(sentence)
                    or not _PERSON.search(sentence) or not _EVENT.search(sentence)):
                continue
            # An event with a specific time/quantity or named object is useful;
            # generic conversational filler is not made into a memory.
            named = re.search(r"\b[A-Z][a-z]{2,}\b", sentence[1:])
            if not (_DETAIL.search(sentence) or named):
                continue
            if sentence in seen or any(sentence in c.content for c in existing):
                continue
            seen.add(sentence)
            decision = GateDecision(
                True, .55, .8, .6, "medium_term", "specific user episode retained as observation",
                GateAction.WRITE, "EPISODIC_OBSERVATION", {"episodic_retention": 1.0}, VERSION,
            )
            result.append(Candidate(sentence, "episode", "OBSERVATION", {
                "subject": "user", "predicate": PREDICATE, "assertion": "reported_episode",
                "role": "user", "source_span": [start, start + len(sentence)],
                "retention_policy": VERSION,
            }, decision, observed_at))
            if len(result) >= self.max_items:
                break
        return result
