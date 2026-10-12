"""One bounded local recall round for missing sides of a compound question.

This is a lexical sufficiency *proxy*, not proof of answerability. It never
reads raw event archives, labels or reference answers and never calls an LLM.
"""
from __future__ import annotations

import re
from typing import Callable, Sequence

from .models import RetrievalItem

_STOP = frozenset("how long many much what which when where did do does have has had i my me we our the a an in on at to of for with and or been was were is it that this before after between by total altogether".split())
_VALUE = re.compile(r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|yesterday|today)\b", re.I)


def terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.casefold()) if w not in _STOP and len(w) > 2}


def required_probes(question: str) -> list[str]:
    if not re.search(r"\b(?:how long|how many|how much|difference|total|earlier|first)\b", question, re.I):
        return []
    pieces = re.split(r"\b(?:when|before|after|and|versus|compared to)\b", question, flags=re.I)
    probes = [" ".join(sorted(terms(p))) for p in pieces if len(terms(p)) >= 2]
    return list(dict.fromkeys(probes))[:2] if len(probes) >= 2 else []


def covered(probe: str, items: Sequence[RetrievalItem]) -> bool:
    asked = terms(probe)
    return any(len(asked & terms(i.memory.content)) >= min(2, len(asked))
               and _VALUE.search(i.memory.content) for i in items)


def recover_evidence(question: str, baseline: Sequence[RetrievalItem],
                     search: Callable[[str], Sequence[RetrievalItem]], *, limit: int = 10):
    probes = required_probes(question)
    missing = [p for p in probes if not covered(p, baseline)]
    fresh: list[RetrievalItem] = []
    seen = {i.memory.id for i in baseline}
    executed = []
    for probe in missing:
        found = list(search(probe))
        executed.append({"query": probe, "returned_ids": [i.memory.id for i in found]})
        accepted = 0
        for item in found:
            if item.memory.id in seen or not covered(probe, [item]):
                continue
            fresh.append(item)
            seen.add(item.memory.id)
            accepted += 1
            if accepted == 2:
                break
    if fresh:
        protected = min(6, max(0, limit - len(fresh)))
        result = [*baseline[:protected], *fresh, *baseline[protected:]][:limit]
    else:
        result = list(baseline)
    return result, {"version": "bounded-premise-recall-v1", "is_answerability_proof": False,
                    "probes": probes, "missing_proxies": missing, "searches": executed,
                    "added_memory_ids": [i.memory.id for i in fresh],
                    "displaced_memory_ids": [i.memory.id for i in baseline if i not in result]}
