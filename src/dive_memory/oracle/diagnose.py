"""Automatic failure diagnosis for an Oracle run.

What this module is *for*
-------------------------

A 500-question run produces 500 traces and one accuracy number. The number says
how bad things are; it does not say *where* to spend the next hour. This module
turns a finished run into a structured failure matrix:

* **Stage attribution** -- the earliest pipeline stage whose failure made a
  correct answer impossible (``FORMATION`` -> ``RETRIEVAL`` -> ``RERANK`` ->
  ``READING``), plus the two abstention-policy outcomes.
* **Abstain root cause** -- a decision list over the same trace, answering
  "why did the reader refuse?" rather than "was the result wrong?".
* **Per-question evidence** -- the trace fields a human needs in order to audit
  one flagged question without re-running the pipeline.

What this module is *not*
-------------------------

**The attribution is a diagnostic heuristic, not ground truth.** It infers the
broken stage from the run's own bookkeeping (which gold turns produced a memory,
which formed memories came back from retrieval, which of those survived
packing). Every inference is labelled with the rule that produced it
(``stage_rule`` / ``abstain_rule``) so a reader can disagree with a specific
rule instead of distrusting the whole table. There is no stage label in the
benchmark that could serve as ground truth for this axis, so nothing here may
be quoted as an accuracy.

Two further limits are structural and are reported rather than papered over:

* ``STORAGE`` is not separable from ``RETRIEVAL`` with the current trace. The
  trace records formed-memory counts per turn and the retrieved provenance, but
  never re-queries the store for a specific gold memory, so "the memory exists
  in the store but the retrieval SQL excluded it" and "the retrieval ranking
  dropped it" cannot be told apart after the fact. The bucket is reported as 0
  with this note instead of being silently folded into ``RETRIEVAL``.
* ``RERANK`` is only observable for memories that retrieval *returned*: an item
  excluded by the packing budget leaves a packing decision behind, an item
  dropped inside the fused ranking does not.

See ``eval/reports/oracle-failure-analysis-v2/README.md`` for the rule table in
prose.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..eval_adapters.longmemeval import LongMemEvalCase
from .contracts import CANONICAL_ABSTENTION_HYPOTHESIS, HypothesisOrigin
from .evaluate import NOT_EVALUATED, NOT_RUN, UNKNOWN, Judge
from .evaluate import NormalizedExactMatchJudge as NormalizedExactMatchJudge
from .loader import load_oracle_dataset

# --------------------------------------------------------------------------- #
# Taxonomies
# --------------------------------------------------------------------------- #


class Stage(StrEnum):
    """Which stage owns a result.

    ``CORRECT`` is a pass, not a failure. ``ABSTAIN`` means the abstention
    *policy* was the decisive cause -- either the reader refused while usable
    evidence was in front of it, or it answered a question it should have
    refused. Upstream evidence problems are attributed to their own stage even
    when the reader also refused, because that is the stage to fix first.
    """

    CORRECT = "CORRECT"
    FORMATION = "FORMATION"
    STORAGE = "STORAGE"
    RETRIEVAL = "RETRIEVAL"
    RERANK = "RERANK"
    READING = "READING"
    ABSTAIN = "ABSTAIN"
    UNKNOWN = "UNKNOWN"


FAILURE_STAGES: tuple[Stage, ...] = (
    Stage.FORMATION,
    Stage.STORAGE,
    Stage.RETRIEVAL,
    Stage.RERANK,
    Stage.READING,
    Stage.ABSTAIN,
    Stage.UNKNOWN,
)


class AbstainCause(StrEnum):
    """Why the reader refused (or why it should have).

    The order of the members is the order of the decision list in
    :func:`classify_abstain`; the first matching rule wins.
    """

    PARSER_OR_STRUCTURED_OUTPUT_FAILURE = "PARSER_OR_STRUCTURED_OUTPUT_FAILURE"
    NO_EVIDENCE_RETRIEVED = "NO_EVIDENCE_RETRIEVED"
    GOLD_EVIDENCE_NOT_FORMED = "GOLD_EVIDENCE_NOT_FORMED"
    WEAK_EVIDENCE = "WEAK_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    TEMPORAL_AMBIGUITY = "TEMPORAL_AMBIGUITY"
    PREFERENCE_AGGREGATION_FAILURE = "PREFERENCE_AGGREGATION_FAILURE"
    ASSISTANT_MEMORY_MISSING = "ASSISTANT_MEMORY_MISSING"
    SUFFICIENT_EVIDENCE_REFUSED = "SUFFICIENT_EVIDENCE_REFUSED"
    UNDER_ABSTENTION_ON_UNANSWERABLE = "UNDER_ABSTENTION_ON_UNANSWERABLE"
    OTHER = "OTHER"


ABSTAIN_CAUSES: tuple[AbstainCause, ...] = tuple(AbstainCause)


# --------------------------------------------------------------------------- #
# Trace access helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceState:
    """What the run's own bookkeeping says about the gold evidence.

    ``turns_with_memory`` is the count of gold evidence turns that produced at
    least one memory, so a question with three gold turns and one formed memory
    is not reported as a clean formation success.
    """

    gold_turns: int
    turns_with_memory: int
    gold_events: frozenset[str]
    returned_gold_memory_ids: frozenset[str]
    packed_gold_memory_ids: frozenset[str]
    returned_any: int
    packed_any: int

    @property
    def gold_formed(self) -> bool:
        return self.turns_with_memory > 0

    @property
    def gold_formed_completely(self) -> bool:
        return self.gold_turns > 0 and self.turns_with_memory == self.gold_turns

    @property
    def gold_retrieved(self) -> bool:
        return bool(self.returned_gold_memory_ids)

    @property
    def gold_packed(self) -> bool:
        return bool(self.packed_gold_memory_ids)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def evaluate_evidence(case: LongMemEvalCase, trace: Mapping[str, Any]) -> EvidenceState:
    """Reconstruct the evidence pipeline for one case from its trace."""

    formation = trace.get("formation") or {}
    counts = formation.get("turn_memory_counts") or {}
    events = formation.get("turn_events") or {}
    gold_turns = list(case.evidence_refs)
    gold_events = {str(events[ref]) for ref in gold_turns if ref in events}
    with_memory = sum(1 for ref in gold_turns if int(counts.get(ref, 0) or 0) > 0)

    retrieval = trace.get("retrieval") or {}
    returned_ids = _as_str_list(retrieval.get("returned_memory_ids"))
    returned_refs = retrieval.get("returned_source_refs") or []
    returned_gold: set[str] = set()
    for index, refs in enumerate(returned_refs):
        if index >= len(returned_ids):
            break
        if gold_events & set(_as_str_list(refs)):
            returned_gold.add(returned_ids[index])

    packing = trace.get("packing") or {}
    packed_ids = set(_as_str_list(packing.get("packed_memory_ids")))
    return EvidenceState(
        gold_turns=len(gold_turns),
        turns_with_memory=with_memory,
        gold_events=frozenset(gold_events),
        returned_gold_memory_ids=frozenset(returned_gold),
        packed_gold_memory_ids=frozenset(returned_gold & packed_ids),
        returned_any=len(returned_ids),
        packed_any=len(packed_ids),
    )


def reader_status(trace: Mapping[str, Any]) -> str:
    return str((trace.get("reader") or {}).get("status") or UNKNOWN)


def reader_reason(trace: Mapping[str, Any]) -> str:
    return str((trace.get("reader") or {}).get("reason_code") or "")


def abstained(trace: Mapping[str, Any], hypothesis: str | None) -> bool:
    """True when the submitted text is the abstention template.

    The template is uniform across every refusal reason (genuine abstention,
    malformed output, timeout, transport error), so it is the only reliable
    signal of "nothing was answered" from the submission alone. ``origin``
    distinguishes the underlying cause and is used elsewhere.
    """

    if str(trace.get("hypothesis_origin") or "") == HypothesisOrigin.READER_ABSTENTION.value:
        return True
    return hypothesis == CANONICAL_ABSTENTION_HYPOTHESIS


def question_temporal_intent(question: str) -> bool:
    """Cheap lexical probe for a question that asks about time.

    Deliberately conservative: it only looks for words that make a *temporal
    comparison* unavoidable. It is a diagnostic grouping, never an input to
    retrieval or to the reader.
    """

    text = str(question).casefold()
    markers = (
        "how long", "how many days", "how many weeks", "how many months",
        "days between", "weeks between", "months between", "before", "after",
        "earlier", "later", "most recent", "latest", "last time", "first",
        "ago", "duration", "how often", "order", "which came first",
        "precede", "follow",
    )
    return any(marker in text for marker in markers)


def _assistant_turns_without_memory(case: LongMemEvalCase, trace: Mapping[str, Any]) -> tuple[int, int]:
    """(assistant evidence turns, of which with no formed memory)."""

    counts = ((trace.get("formation") or {}).get("turn_memory_counts")) or {}
    assistant_gold = [
        turn.ref for turn in case.turns if turn.has_answer and turn.role.casefold() != "user"
    ]
    without = [ref for ref in assistant_gold if int(counts.get(ref, 0) or 0) == 0]
    return len(assistant_gold), len(without)


def _has_conflict_group(trace: Mapping[str, Any]) -> bool:
    decisions = ((trace.get("packing") or {}).get("decisions")) or []
    packed = set(_as_str_list((trace.get("packing") or {}).get("packed_memory_ids")))
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        if "conflict_group" in json.dumps(decision):
            return True
    context = str(trace.get("context_text") or "")
    return "conflict_group=" in context and bool(packed)


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CaseDiagnosis:
    """One question's diagnosis, with the rule that produced it."""

    question_id: str
    question_type: str
    should_abstain: bool
    stage: Stage
    stage_rule: str
    abstain_cause: AbstainCause | None
    abstain_rule: str
    judged: bool | None
    reader_status: str
    reader_behavior: str
    evidence: EvidenceState
    diagnosis_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_type": self.question_type,
            "should_abstain": self.should_abstain,
            "stage": self.stage.value,
            "stage_rule": self.stage_rule,
            "abstain_cause": self.abstain_cause.value if self.abstain_cause else None,
            "abstain_rule": self.abstain_rule,
            "judged_correct": self.judged,
            "reader_status": self.reader_status,
            "reader_behavior": self.reader_behavior,
            "gold_evidence_turns": self.evidence.gold_turns,
            "gold_turns_with_memory": self.evidence.turns_with_memory,
            "gold_formed": self.evidence.gold_formed,
            "gold_formed_completely": self.evidence.gold_formed_completely,
            "gold_retrieved": self.evidence.gold_retrieved,
            "gold_packed": self.evidence.gold_packed,
            "retrieved_items": self.evidence.returned_any,
            "packed_items": self.evidence.packed_any,
            "diagnosis_notes": list(self.diagnosis_notes),
        }


def classify_stage(
    *,
    should_abstain: bool,
    judged: bool | None,
    refused: bool,
    status: str,
    evidence: EvidenceState,
) -> tuple[Stage, str]:
    """Return the owning stage and the name of the rule that decided it."""

    if judged is None:
        return Stage.UNKNOWN, "judge_unavailable"

    if should_abstain:
        if judged:
            return Stage.CORRECT, "unanswerable_and_refused"
        return Stage.ABSTAIN, "answered_an_unanswerable_question"

    if not evidence.gold_turns:
        # An answerable question with no gold turn is a dataset/annotation
        # property, not a pipeline failure; it cannot be attributed to a stage.
        return (Stage.CORRECT if judged else Stage.UNKNOWN), "no_gold_turns_published"

    if judged and evidence.gold_packed:
        return Stage.CORRECT, "gold_packed_and_correct"

    if not evidence.gold_formed:
        return Stage.FORMATION, "no_gold_turn_produced_a_memory"

    if not evidence.gold_retrieved:
        return Stage.RETRIEVAL, "gold_memory_exists_but_was_not_returned"

    if not evidence.gold_packed:
        return Stage.RERANK, "gold_memory_returned_but_dropped_before_the_reader"

    if refused:
        return Stage.ABSTAIN, "gold_evidence_packed_but_reader_refused"

    if status in {"INVALID", "ERROR", "TIMEOUT"}:
        return Stage.READING, f"reader_{status.lower()}"

    return Stage.READING, "gold_evidence_packed_and_answer_still_wrong"


def classify_abstain(
    case: LongMemEvalCase,
    trace: Mapping[str, Any],
    evidence: EvidenceState,
    *,
    should_abstain: bool,
    judged: bool | None,
) -> tuple[AbstainCause | None, str]:
    """Explain a refusal, or an answer that should have been a refusal.

    Returns ``(None, "not_an_abstain_event")`` when the abstention policy is not
    implicated at all.
    """

    status = reader_status(trace)
    refused = abstained(trace, str(trace.get("hypothesis") or ""))

    if should_abstain:
        if judged is False:
            return (AbstainCause.UNDER_ABSTENTION_ON_UNANSWERABLE,
                    "answered_a_question_the_dataset_marks_unanswerable")
        return None, "not_an_abstain_event"

    if not refused:
        return None, "not_an_abstain_event"

    if status in {"INVALID", "ERROR", "TIMEOUT"}:
        return (AbstainCause.PARSER_OR_STRUCTURED_OUTPUT_FAILURE,
                f"reader_status={status}")

    if evidence.returned_any == 0:
        return AbstainCause.NO_EVIDENCE_RETRIEVED, "retrieval_returned_zero_items"

    if evidence.gold_packed:
        if _has_conflict_group(trace):
            return (AbstainCause.CONFLICTING_EVIDENCE,
                    "packed_context_carries_a_conflict_group")
        if case.question_type == "single-session-preference":
            return (AbstainCause.PREFERENCE_AGGREGATION_FAILURE,
                    "preference_question_with_packed_gold_evidence")
        if case.question_type == "temporal-reasoning" or question_temporal_intent(case.question):
            return (AbstainCause.TEMPORAL_AMBIGUITY,
                    "temporal_question_with_packed_gold_evidence")
        return (AbstainCause.SUFFICIENT_EVIDENCE_REFUSED,
                "gold_evidence_was_packed_and_the_reader_still_refused")

    if not evidence.gold_formed:
        assistant_total, assistant_missing = _assistant_turns_without_memory(case, trace)
        if assistant_total and assistant_missing == assistant_total:
            return (AbstainCause.ASSISTANT_MEMORY_MISSING,
                    "every_assistant_evidence_turn_failed_to_form_a_memory")
        return (AbstainCause.GOLD_EVIDENCE_NOT_FORMED,
                "no_gold_turn_produced_a_memory")

    if case.question_type == "temporal-reasoning" or question_temporal_intent(case.question):
        return (AbstainCause.TEMPORAL_AMBIGUITY,
                "temporal_question_and_gold_evidence_absent_from_context")

    if case.question_type == "single-session-preference":
        return (AbstainCause.PREFERENCE_AGGREGATION_FAILURE,
                "preference_question_and_gold_evidence_absent_from_context")

    if case.question_type == "single-session-assistant":
        return (AbstainCause.ASSISTANT_MEMORY_MISSING,
                "assistant_question_and_gold_evidence_absent_from_context")

    return AbstainCause.WEAK_EVIDENCE, "retrieved_items_do_not_trace_to_a_gold_turn"


def diagnose_case(
    case: LongMemEvalCase,
    trace: Mapping[str, Any] | None,
    *,
    hypothesis: str | None,
    judged: bool | None,
) -> CaseDiagnosis:
    """Diagnose one question. Never raises for a partial trace."""

    trace = trace or {}
    evidence = evaluate_evidence(case, trace)
    status = reader_status(trace)
    refused = abstained(trace, hypothesis)
    stage, stage_rule = classify_stage(
        should_abstain=case.should_abstain,
        judged=judged,
        refused=refused,
        status=status,
        evidence=evidence,
    )
    cause, reason_rule = classify_abstain(
        case, trace, evidence, should_abstain=case.should_abstain, judged=judged
    )
    notes: list[str] = []
    if not trace:
        notes.append("no trace file was found for this question")
    if evidence.gold_turns and not evidence.gold_formed_completely and evidence.gold_formed:
        notes.append(
            f"partial formation: {evidence.turns_with_memory}/{evidence.gold_turns} "
            "gold evidence turns produced a memory"
        )
    behavior = "abstained" if refused else ("error" if status in {"INVALID", "ERROR", "TIMEOUT"} else "answered")
    return CaseDiagnosis(
        question_id=case.question_id,
        question_type=case.question_type,
        should_abstain=case.should_abstain,
        stage=stage,
        stage_rule=stage_rule,
        abstain_cause=cause,
        abstain_rule=reason_rule,
        judged=judged,
        reader_status=status,
        reader_behavior=behavior,
        evidence=evidence,
        diagnosis_notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# Matrix assembly
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TypeBlock:
    """Section A: one question type."""

    question_type: str
    total: int = 0
    answered: int = 0
    abstain: int = 0
    correct: int = 0
    incorrect: int = 0
    unjudged: int = 0
    gold_formed: int = 0
    gold_retrieved: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "answered": self.answered,
            "abstain": self.abstain,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "unjudged": self.unjudged,
            "accuracy_all_cases": (self.correct / self.total) if self.total else None,
            "accuracy_answered_only": (self.correct / self.answered) if self.answered else None,
            "gold_formed_rate": (self.gold_formed / self.total) if self.total else None,
            "gold_retrieved_rate": (self.gold_retrieved / self.total) if self.total else None,
            "denominator_all_cases": self.total,
            "denominator_answered_only": self.answered,
        }


@dataclass(slots=True)
class StageBlock:
    """Section B: one stage, sliced by question type."""

    stage: Stage
    total: int = 0
    by_question_type: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.total,
            "share_of_all_cases": None,  # filled by the caller that knows the total
            "by_question_type": dict(sorted(self.by_question_type.items())),
        }


def _load_traces(traces_dir: Path) -> dict[str, dict[str, Any]]:
    traces: dict[str, dict[str, Any]] = {}
    for path in sorted(traces_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        question_id = record.get("question_id")
        if isinstance(question_id, str):
            traces[question_id] = record
    return traces


def _load_submission(path: Path) -> dict[str, str]:
    hypotheses: dict[str, str] = {}
    if not path.exists():
        return hypotheses
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        question_id = row.get("question_id")
        if isinstance(question_id, str):
            hypotheses[question_id] = str(row.get("hypothesis", ""))
    return hypotheses


def _trace_export(
    case: LongMemEvalCase, trace: Mapping[str, Any], diag: CaseDiagnosis,
) -> dict[str, Any]:
    """Section C: everything an auditor needs for one flagged question."""

    retrieval = trace.get("retrieval") or {}
    packing = trace.get("packing") or {}
    reader = trace.get("reader") or {}
    return {
        "question_id": case.question_id,
        "question": case.question,
        "question_type": case.question_type,
        "question_date": case.question_date,
        "should_abstain": case.should_abstain,
        "reader_status": diag.reader_status,
        "reader_behavior": diag.reader_behavior,
        "abstain_reason_code": reader.get("reason_code"),
        "abstain_missing_requirements": list(reader.get("missing_requirements") or []),
        "retrieved_memory_ids": _as_str_list(retrieval.get("returned_memory_ids")),
        "retrieved_source_refs": retrieval.get("returned_source_refs") or [],
        "retrieval_channels": retrieval.get("executed_channels") or [],
        "retrieval_empty": retrieval.get("empty"),
        "retrieval_temporal_mode": retrieval.get("temporal_mode"),
        "packed_memory_ids": _as_str_list(packing.get("packed_memory_ids")),
        "packing_decisions": packing.get("decisions") or [],
        "packing_current_only": packing.get("current_only"),
        "context_text": trace.get("context_text"),
        "formation": {
            "turns_offered": (trace.get("formation") or {}).get("turns_offered"),
            "turns_ingested": (trace.get("formation") or {}).get("turns_ingested"),
            "memories_formed": (trace.get("formation") or {}).get("memories_formed"),
            "turn_memory_counts": (trace.get("formation") or {}).get("turn_memory_counts"),
            "turn_roles": {turn.ref: turn.role for turn in case.turns},
            "turn_occurred_at": {turn.ref: turn.occurred_at for turn in case.turns},
        },
        "evidence": {
            "gold_turn_refs": list(case.evidence_refs),
            "gold_session_ids": list(case.evidence_session_ids),
            "gold_events": sorted(diag.evidence.gold_events),
            "returned_gold_memory_ids": sorted(diag.evidence.returned_gold_memory_ids),
            "packed_gold_memory_ids": sorted(diag.evidence.packed_gold_memory_ids),
            "gold_formed": diag.evidence.gold_formed,
            "gold_retrieved": diag.evidence.gold_retrieved,
            "gold_packed": diag.evidence.gold_packed,
        },
        "diagnosis": {
            "stage": diag.stage.value,
            "stage_rule": diag.stage_rule,
            "abstain_cause": diag.abstain_cause.value if diag.abstain_cause else None,
            "abstain_rule": diag.abstain_rule,
            "notes": list(diag.diagnosis_notes),
            "is_diagnostic_heuristic": True,
        },
        "hypothesis": trace.get("hypothesis"),
        "reference_answer": case.answer,
        "judged_correct": diag.judged,
        "timing_ms": trace.get("timing_ms"),
        "tokens": trace.get("tokens"),
    }


def build_matrix(
    *,
    dataset_path: str | Path,
    run_dir: str | Path,
    judge: Judge | None = None,
    examples: int = 0,
) -> dict[str, Any]:
    """Compute the full failure matrix for one finished run."""

    run = Path(run_dir)
    dataset = load_oracle_dataset(dataset_path, expected_cases=None)
    cases = list(dataset.cases)
    traces = _load_traces(run / "traces")
    hypotheses = _load_submission(run / "submission.jsonl")

    diagnosed: list[CaseDiagnosis] = []
    for case in cases:
        hypothesis = hypotheses.get(case.question_id)
        verdict = None
        if judge is not None and hypothesis is not None:
            verdict = judge.judge(
                question=case.question,
                reference=case.answer,
                hypothesis=hypothesis,
                question_type="abstention" if case.should_abstain else case.question_type,
            )
        diagnosed.append(diagnose_case(
            case, traces.get(case.question_id),
            hypothesis=hypothesis,
            judged=verdict.correct if verdict else None,
        ))

    total = len(diagnosed)
    by_type: dict[str, TypeBlock] = {}
    for diag in diagnosed:
        block = by_type.setdefault(diag.question_type, TypeBlock(diag.question_type))
        block.total += 1
        if diag.reader_behavior == "abstained":
            block.abstain += 1
        else:
            block.answered += 1
        if diag.judged is None:
            block.unjudged += 1
        elif diag.judged:
            block.correct += 1
        else:
            block.incorrect += 1
        block.gold_formed += int(diag.evidence.gold_formed)
        block.gold_retrieved += int(diag.evidence.gold_retrieved)

    stage_blocks: dict[str, StageBlock] = {stage.value: StageBlock(stage) for stage in Stage}
    for diag in diagnosed:
        block = stage_blocks[diag.stage.value]
        block.total += 1
        block.by_question_type[diag.question_type] += 1

    abstain_cause_counts: Counter[str] = Counter()
    for diag in diagnosed:
        if diag.abstain_cause is not None:
            abstain_cause_counts[diag.abstain_cause.value] += 1

    answered = sum(1 for d in diagnosed if d.reader_behavior != "abstained")
    refusals = sum(1 for d in diagnosed if d.reader_behavior == "abstained")
    judged_rows = [d for d in diagnosed if d.judged is not None]
    correct = sum(1 for d in diagnosed if d.judged)
    abstention_rows = [d for d in diagnosed if d.should_abstain]

    stage_note = {
        "STORAGE": (
            "not separable from RETRIEVAL with the current trace: the run never re-queries "
            "the store for a specific gold memory, so 'excluded by the store' cannot be told "
            "apart from 'dropped by the ranking'. Reported as 0 rather than folded into "
            "RETRIEVAL."
        ),
        "RERANK": (
            "only observable for memories retrieval returned: a packing-budget exclusion "
            "leaves a decision behind, a fused-ranking drop does not."
        ),
        "ABSTAIN": (
            "the abstention policy was the decisive cause -- either the reader refused with "
            "usable evidence in front of it, or it answered an unanswerable question."
        ),
        "UNKNOWN": (
            "the judge produced no verdict, or an answerable question published no gold turn."
        ),
        "heuristic": (
            "Stage and abstain cause are inferred from the run's own bookkeeping. There is no "
            "benchmark label for this axis, so neither field is ground truth and neither may "
            "be reported as an accuracy."
        ),
    }

    matrix = {
        "schema_version": "dive-oracle-failure-matrix-v2",
        "attribution_notice": stage_note["heuristic"],
        "dataset": {
            "name": dataset.declared_name,
            "path": str(dataset.path),
            "sha256": dataset.sha256,
            "cases": len(cases),
        },
        "run": {
            "run_dir": str(run),
            "traces_found": len(traces),
            "submission_lines": len(hypotheses),
            "judge": getattr(getattr(judge, "spec", None), "model", NOT_RUN),
        },
        "headline": {
            "cases_total": total,
            "answered": answered,
            "abstain": refusals,
            "abstain_rate": refusals / total if total else None,
            "correct_all_cases": correct,
            "full_set_accuracy": correct / total if total else None,
            "answered_only_accuracy": correct / answered if answered else None,
            "judged": len(judged_rows),
            "unjudged": total - len(judged_rows),
            "denominator_full_set": total,
            "denominator_answered_only": answered,
            "denominator_judged": len(judged_rows),
            "official_score": NOT_EVALUATED,
        },
        "by_question_type": {name: block.to_dict() for name, block in sorted(by_type.items())},
        "by_failure_stage": {
            name: {**block.to_dict(),
                   "share_of_all_cases": (block.total / total) if total else None,
                   "note": stage_note.get(name)}
            for name, block in stage_blocks.items()
        },
        "abstain_analysis": {
            "abstain_rate": refusals / total if total else None,
            "abstain_count": refusals,
            "answered_count": answered,
            "abstain_root_causes": {
                cause.value: abstain_cause_counts.get(cause.value, 0)
                for cause in ABSTAIN_CAUSES
            },
            "abstain_on_answerable_questions": sum(
                1 for d in diagnosed if d.reader_behavior == "abstained" and not d.should_abstain
            ),
            "abstain_on_unanswerable_questions": sum(
                1 for d in diagnosed if d.reader_behavior == "abstained" and d.should_abstain
            ),
            "unanswerable_questions": len(abstention_rows),
            "unanswerable_refused_correctly": sum(
                1 for d in abstention_rows if d.judged
            ),
            "abstain_precision_on_refusals": (
                sum(1 for d in diagnosed if d.reader_behavior == "abstained" and d.should_abstain)
                / refusals if refusals else None
            ),
            "note": (
                "abstain_root_causes is a decision list evaluated in the order listed in "
                "dive_memory.oracle.diagnose.classify_abstain; the first matching rule wins. "
                "It is a diagnostic grouping."
            ),
        },
        "stage_rules": sorted({d.stage_rule for d in diagnosed}),
        "abstain_rules": sorted({d.abstain_rule for d in diagnosed}),
    }
    if examples:
        matrix["examples"] = [
            d.to_dict() for d in diagnosed if d.stage is not Stage.CORRECT
        ][:examples]
    return matrix


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #


def write_artifacts(
    *,
    matrix: Mapping[str, Any],
    dataset_path: str | Path,
    run_dir: str | Path,
    output_dir: str | Path,
    judge: Judge | None = None,
) -> dict[str, str]:
    """Write the six artefacts the analysis stage publishes."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    run = Path(run_dir)
    cases = {case.question_id: case for case in load_oracle_dataset(dataset_path, expected_cases=None).cases}
    traces = _load_traces(run / "traces")
    hypotheses = _load_submission(run / "submission.jsonl")

    written: dict[str, str] = {}

    def dump(name: str, payload: Any) -> None:
        path = out / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written[name] = str(path)

    dump("summary.json", matrix)
    dump("by_question_type.json", {
        "schema_version": "dive-oracle-failure-matrix-v2",
        "denominator_all_cases": matrix["headline"]["denominator_full_set"],
        "by_question_type": matrix["by_question_type"],
    })
    dump("by_failure_stage.json", {
        "schema_version": "dive-oracle-failure-matrix-v2",
        "attribution_notice": matrix["attribution_notice"],
        "by_failure_stage": matrix["by_failure_stage"],
        "stage_rules_observed": matrix["stage_rules"],
    })
    dump("abstain_analysis.json", {
        "schema_version": "dive-oracle-failure-matrix-v2",
        "abstain_analysis": matrix["abstain_analysis"],
        "abstain_rules_observed": matrix["abstain_rules"],
    })

    failures_path = out / "failure_cases.jsonl"
    lines: list[str] = []
    for question_id, case in cases.items():
        trace = traces.get(question_id) or {}
        hypothesis = hypotheses.get(question_id)
        verdict = None
        if judge is not None and hypothesis is not None:
            verdict = judge.judge(
                question=case.question,
                reference=case.answer,
                hypothesis=hypothesis,
                question_type="abstention" if case.should_abstain else case.question_type,
            )
        diag = diagnose_case(
            case, trace, hypothesis=hypothesis, judged=verdict.correct if verdict else None
        )
        if diag.stage is Stage.CORRECT:
            continue
        lines.append(json.dumps(_trace_export(case, trace, diag), ensure_ascii=False))
    failures_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    written["failure_cases.jsonl"] = str(failures_path)
    return written


def write_readme(path: str | Path, *, matrix: Mapping[str, Any], run_dir: str) -> str:
    """Write the prose companion: what the rules are and what they cannot say."""

    target = Path(path)
    headline = matrix["headline"]
    stages = matrix["by_failure_stage"]

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{100 * float(value):.1f}%"

    lines = [
        "# Oracle failure matrix v2",
        "",
        f"Source run: `{run_dir}`  ",
        f"Judged cases: {headline['judged']} / {headline['cases_total']} "
        f"(`official_score = NOT_EVALUATED`; every number here is self-reported).",
        "",
        "## Headline",
        "",
        "| metric | value | denominator |",
        "| --- | --- | --- |",
        f"| abstain rate | {pct(headline['abstain_rate'])} | {headline['denominator_full_set']} |",
        f"| full-set accuracy | {pct(headline['full_set_accuracy'])} | "
        f"{headline['denominator_full_set']} |",
        f"| answered-only accuracy | {pct(headline['answered_only_accuracy'])} | "
        f"{headline['denominator_answered_only']} |",
        "",
        "`full_set_accuracy` counts an abstention as a miss and uses every question as the",
        "denominator. `answered_only_accuracy` is reported next to it, never instead of it: a",
        "system that refuses more looks better on the second number while getting worse on the",
        "first.",
        "",
        "## How the stage attribution works",
        "",
        "The rules are evaluated in order and the first match wins.",
        "",
        "| # | condition | stage |",
        "| --- | --- | --- |",
        "| 1 | judge returned no verdict | UNKNOWN |",
        "| 2 | unanswerable question, judge says correct | CORRECT |",
        "| 3 | unanswerable question, answered anyway | ABSTAIN |",
        "| 4 | answerable question publishes no gold turn | CORRECT / UNKNOWN |",
        "| 5 | gold evidence packed and the answer is correct | CORRECT |",
        "| 6 | no gold evidence turn produced a memory | FORMATION |",
        "| 7 | gold memory exists but retrieval did not return it | RETRIEVAL |",
        "| 8 | gold memory returned but dropped before the reader | RERANK |",
        "| 9 | gold evidence packed and the reader refused | ABSTAIN |",
        "| 10 | reader produced INVALID / ERROR / TIMEOUT | READING |",
        "| 11 | otherwise | READING |",
        "",
        "**This is a diagnostic heuristic, not ground truth.** There is no benchmark label for",
        "the failure stage, so the table is a way to allocate attention, not a measurement.",
        "Each row in `failure_cases.jsonl` carries the name of the rule that fired",
        "(`diagnosis.stage_rule`) so a single row can be disputed without disputing the table.",
        "",
        "### What the taxonomy cannot separate",
        "",
    ]
    for name in ("STORAGE", "RERANK", "ABSTAIN", "UNKNOWN"):
        note = (stages.get(name) or {}).get("note")
        if note:
            lines.append(f"* **{name}** ({stages[name]['count']} cases) -- {note}")
    lines += [
        "",
        "## How the abstain causes are assigned",
        "",
        "`classify_abstain` is a decision list; the first matching rule wins and the rule name is",
        "stored per case in `abstain_analysis.json` consumers (`abstain_rule` in",
        "`failure_cases.jsonl`).",
        "",
        "| # | condition | cause |",
        "| --- | --- | --- |",
        "| 1 | reader status INVALID / ERROR / TIMEOUT | PARSER_OR_STRUCTURED_OUTPUT_FAILURE |",
        "| 2 | retrieval returned zero items | NO_EVIDENCE_RETRIEVED |",
        "| 3 | packed context carries a conflict group | CONFLICTING_EVIDENCE |",
        "| 4 | preference question, gold evidence packed | PREFERENCE_AGGREGATION_FAILURE |",
        "| 5 | temporal question, gold evidence packed | TEMPORAL_AMBIGUITY |",
        "| 6 | gold evidence packed, reader still refused | SUFFICIENT_EVIDENCE_REFUSED |",
        "| 7 | every assistant evidence turn failed to form a memory | ASSISTANT_MEMORY_MISSING |",
        "| 8 | no gold turn produced a memory | GOLD_EVIDENCE_NOT_FORMED |",
        "| 9 | temporal question, gold evidence not in context | TEMPORAL_AMBIGUITY |",
        "| 10 | preference question, gold evidence not in context | PREFERENCE_AGGREGATION_FAILURE |",
        "| 11 | assistant question, gold evidence not in context | ASSISTANT_MEMORY_MISSING |",
        "| 12 | otherwise | WEAK_EVIDENCE |",
        "",
        "## Files",
        "",
        "* `summary.json` -- the whole matrix in one document.",
        "* `by_question_type.json` -- section A, one block per question type.",
        "* `by_failure_stage.json` -- section B, one block per stage with the question-type slice.",
        "* `abstain_analysis.json` -- abstain rate, root-cause distribution, refusal precision.",
        "* `failure_cases.jsonl` -- one JSON object per non-correct question, carrying the trace",
        "  fields needed to audit it (retrieved ids and provenance, packing decisions, packed",
        "  context, per-turn roles, gold-evidence state, the fired rules, and the reference",
        "  answer for scoring only).",
        "",
        "## Reproduction",
        "",
        "```",
        "python -m dive_memory.oracle.diagnose \\",
        "    --dataset eval/external/longmemeval/longmemeval_oracle.json \\",
        f"    --run-dir {run_dir} \\",
        "    --output-dir eval/reports/oracle-failure-analysis-v2 \\",
        "    --judge exact",
        "```",
        "",
        "The reference answer is read only after the run finished, and only to score it; it",
        "never reaches ingest, retrieval or the reader.",
        "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")
    return str(target)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dive_memory.oracle.diagnose",
        description="Build the structured failure matrix for a finished Oracle run.",
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-dir", required=True,
                        help="run directory holding traces/, submission.jsonl, summary.json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--judge", choices=("exact", "openai", "none"), default="exact")
    parser.add_argument("--judge-endpoint")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-api-key-env", default="DIVE_JUDGE_API_KEY")
    parser.add_argument("--examples", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    from .evaluate import build_judge  # local import keeps the CLI dependency-free

    args = build_parser().parse_args(argv)
    judge = build_judge(args)
    matrix = build_matrix(
        dataset_path=args.dataset,
        run_dir=args.run_dir,
        judge=judge,
        examples=max(0, args.examples),
    )
    written = write_artifacts(
        matrix=matrix,
        dataset_path=args.dataset,
        run_dir=args.run_dir,
        output_dir=args.output_dir,
        judge=judge,
    )
    written["README.md"] = write_readme(
        Path(args.output_dir) / "README.md", matrix=matrix, run_dir=str(args.run_dir)
    )
    print(json.dumps({
        "outputs": written,
        "abstain_rate": matrix["headline"]["abstain_rate"],
        "full_set_accuracy": matrix["headline"]["full_set_accuracy"],
        "answered_only_accuracy": matrix["headline"]["answered_only_accuracy"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
