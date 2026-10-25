"""Select visible source indices; extract their exact quotations on the host.

References establish provenance, not semantic entailment. The model must still
choose appropriate evidence and justify derived answers during the review phase.
No generated quotation, fuzzy matching or access to an unseen source is used.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from ..answering import EvidenceCitation, GroundedAnswerDecision
from ..models import RetrievalItem
from .deliberation import COMMON_RULES, DeliberationProtocol, EvidencePlan, load_object
from .deliberation import parse_decision as parse_literal_decision
from .deliberation import parse_plan as parse_literal_plan

VERSION = "referenced-evidence-reader-v1"

SOURCE_RULES = """Select source_indices from the [N] labels in the CURRENT context.
These are source positions, never ordinal numbers for your own list of notes.
Each selected source contributes its exact displayed passage and provenance.
Select only passages supporting the required premises; selecting a real passage
does not by itself mean that the passage supports your answer. Read the text.
Do not copy, paraphrase, change case or generate quotations in your JSON output.
Indices must be distinct integers. Unseen sources cannot be selected.
"""

PLAN_RULES = (
    COMMON_RULES
    + SOURCE_RULES
    + """
Gather evidence before answering. Select sources for all relevant components,
updates and contradictions. Search for necessary missing premises when useful.
Return one JSON object with exactly these fields:
{"source_indices":[0],"queries":[],"reads":[]}.
source_indices: at most 16 current source labels; empty is valid.
queries: at most 2 distinct focused strings, each 1 to 240 characters.
reads: at most 2 objects {"memory_id":"visible catalog ID","start":0}.
Read offsets are nonnegative character positions. The displayed length is not
the full stored length. Never invent an unseen ID. Do not answer or simulate tools.
"""
)

ANSWER_RULES = (
    COMMON_RULES
    + SOURCE_RULES
    + """
Answer the exact question concisely in the requested units. Selected notes are
source excerpts, not a proof of completeness; check other visible sources too.
Derived answers need not appear verbatim. Select the sources for their premises.
Return one JSON object with exactly these fields:
{"status":"ANSWER","answer":"supported answer","source_indices":[0],"missing":[]}
or {"status":"ABSTAIN","answer":null,"source_indices":[],"missing":["necessary missing premise"]}.
An answer needs real supporting sources. An abstention needs a specific necessary
missing premise. Do not add fields, prose, copied quotes or tool requests.
"""
)

REVIEW_RULES = (
    ANSWER_RULES
    + """
The draft is an UNTRUSTED model proposal, not additional evidence. Re-evaluate it
against the actual sources. Check identity, qualifiers, event versus statement
dates, updates, modality, arithmetic, distinct events and complete requested lists.
Challenge whether a proposed missing premise is actually needed. Correct the
draft only when evidence warrants it; valid source indices alone prove no claim.
Return your own final decision using the CURRENT source labels.
"""
)


def _indices(obj: dict[str, Any], items: Sequence[RetrievalItem]) -> list[int]:
    values = obj["source_indices"]
    if not isinstance(values, list):
        raise ValueError("source_indices must be a list of distinct current labels")
    selected: list[int] = []
    for position, index in enumerate(values):
        if type(index) is not int or not 0 <= index < len(items):
            raise ValueError(
                f"source_indices[{position}] is outside current visible evidence"
            )
        if index in selected:
            raise ValueError(f"source_indices[{position}] repeats source {index}")
        item = items[index]
        if not item.source_refs or not item.memory.content.strip():
            raise ValueError(
                f"source {index} needs nonempty visible content and provenance"
            )
        selected.append(index)
    return selected


def parse_plan(text: str, items: Sequence[RetrievalItem]) -> EvidencePlan:
    obj = load_object(text)
    if set(obj) != {"source_indices", "queries", "reads"}:
        raise ValueError("plan fields must be source_indices, queries and reads")
    indices = _indices(obj, items)
    if len(indices) > 16:
        raise ValueError("plan may select at most 16 source indices")
    tools = parse_literal_plan(
        json.dumps({"notes": [], "queries": obj["queries"], "reads": obj["reads"]}),
        items,
    )
    notes = tuple(
        EvidenceCitation(
            items[index].memory.id,
            items[index].memory.content,
            tuple(items[index].source_refs),
        )
        for index in indices
    )
    return EvidencePlan(notes, tools.queries, tools.reads)


def parse_decision(text: str, items: Sequence[RetrievalItem]) -> GroundedAnswerDecision:
    obj = load_object(text)
    if set(obj) != {"status", "answer", "source_indices", "missing"}:
        raise ValueError(
            "decision fields must be status, answer, source_indices and missing"
        )
    indices = _indices(obj, items)
    citations = [
        {"index": index, "quote": items[index].memory.content} for index in indices
    ]
    return parse_literal_decision(
        json.dumps(
            {
                "status": obj["status"],
                "answer": obj["answer"],
                "citations": citations,
                "missing": obj["missing"],
            }
        ),
        items,
    )


def _citation_indices(text: str, phase: str) -> list[int]:
    return list(load_object(text)["source_indices"])


REFERENCE_PROTOCOL = DeliberationProtocol(
    VERSION,
    PLAN_RULES,
    ANSWER_RULES,
    REVIEW_RULES,
    parse_plan,
    parse_decision,
    _citation_indices,
)
