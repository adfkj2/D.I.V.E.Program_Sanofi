"""Formal analysis of an Oracle run: accuracy, attribution, latency, cost.

Two different numbers come out of a LongMemEval run and they must never be
conflated:

* **Self-measured accuracy** -- what this module computes from the run's traces
  using whatever judge the caller injects. Useful for iteration. Its status is
  always ``SELF_REPORTED_NON_OFFICIAL``.
* **The official Overall Accuracy** -- produced only when the project group
  re-runs the submission with the published ``evaluate_qa.py`` and its own judge
  model against the same reference answers. That number cannot be produced
  locally, so ``official_score`` is always ``NOT_EVALUATED`` here and the report
  says so in words.

Everything that was not measured is reported as ``NOT_RUN``,
``NOT_EVALUATED`` or ``UNKNOWN`` rather than omitted or estimated.

See ``docs/oracle/03-cost-estimation.md`` and ``docs/oracle/04-failure-taxonomy.md``.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .contracts import (
    ABSTENTION_SUFFIX,
    CANONICAL_ABSTENTION_HYPOTHESIS,
    ORACLE_EXPECTED_CASES,
    HypothesisOrigin,
)
from .loader import load_oracle_dataset
from .validator import read_submission, validate_submission

NOT_RUN = "NOT_RUN"
NOT_EVALUATED = "NOT_EVALUATED"
UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Judges
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    provider: str
    model: str
    revision: str | None = None
    prompt_version: str = "longmemeval-unofficial-anscheck-v1"
    temperature: float = 0.0
    max_tokens: int = 256
    timeout_seconds: float = 60.0
    official: bool = False
    caveat: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    correct: bool | None
    raw: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    error: str | None = None


class Judge(Protocol):
    spec: JudgeSpec

    def judge(self, *, question: str, reference: str, hypothesis: str,
              question_type: str) -> JudgeVerdict: ...


_REFUSAL_MARKERS = (
    "not enough", "insufficient", "cannot determine", "can't determine",
    "does not contain", "doesn't contain", "no information", "unable to",
    "not specified", "not mentioned", "did not mention", "didn't mention",
    "i don't know", "i do not know", "no record",
)

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}


def normalize_answer(text: str) -> str:
    """Light normalisation for the deterministic judge only.

    This is *not* the official protocol: the official judge is an LLM that
    tolerates paraphrase. Normalising here is a wiring check, and the report
    labels any accuracy derived from it as non-official.
    """

    lowered = str(text).casefold()
    lowered = _PUNCT.sub(" ", lowered)
    tokens = [_NUMBER_WORDS.get(token, token) for token in lowered.split()]
    return _WHITESPACE.sub(" ", " ".join(tokens)).strip()


def looks_like_refusal(text: str) -> bool:
    lowered = str(text).casefold()
    if CANONICAL_ABSTENTION_HYPOTHESIS.casefold() in lowered:
        return True
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


@dataclass(slots=True)
class NormalizedExactMatchJudge:
    """Strict normalized equality check. Never official.

    It exists so the pipeline can be exercised without a credential and so a
    regression in hypothesis extraction shows up as a score change. It cannot
    see paraphrase and is not a calibrated bound on official accuracy. The old
    v1 token-containment rule even accepted contradictory sentences that merely
    repeated all words of the reference answer; v2 removes that rule.
    """

    spec: JudgeSpec = field(default_factory=lambda: JudgeSpec(
        provider="deterministic",
        model="normalized-exact-match-v2",
        prompt_version="normalized-exact-match-v2",
        official=False,
        caveat=(
            "Deterministic string comparison over normalised answers. Not the official "
            "LLM judge: it rejects valid paraphrase and cannot score partial credit."
        ),
    ))
    calls: int = 0

    def judge(self, *, question: str, reference: str, hypothesis: str,
              question_type: str) -> JudgeVerdict:
        self.calls += 1
        is_abstention = question_type == "abstention" or str(question).strip() == ""
        if is_abstention:
            correct = looks_like_refusal(hypothesis)
            return JudgeVerdict(bool(correct), "REFUSAL" if correct else "ANSWERED")
        gold = normalize_answer(reference)
        guess = normalize_answer(hypothesis)
        if not gold or not guess:
            return JudgeVerdict(False, "EMPTY")
        if gold == guess:
            return JudgeVerdict(True, "EXACT")
        return JudgeVerdict(False, "MISMATCH")


def _anscheck_instruction(question_type: str) -> str:
    """Return the per-question-type judging rubric.

    Mirrors the LongMemEval reference ``get_anscheck_prompt`` task-specific rubrics so an
    unofficially configured LLM judge (e.g. DeepSeek) behaves as close as possible to the
    official gpt-4o judge. The ordinary case stays a strict semantic-equivalence check; the
    four specialised rubrics relax scoring exactly where the official benchmark does.
    """
    rubric = {
        "temporal-reasoning": (
            "This is a temporal-reasoning question. Decide whether the model answer gives the "
            "same date, duration or time period as the reference answer. Tolerate off-by-one "
            "errors (e.g. 18 days vs 19 days, 1 week vs 8 days) and equivalent formats. Answer "
            "yes if they refer to the same time point or interval, no otherwise."
        ),
        "knowledge-update": (
            "This is a knowledge-update question. The correct answer reflects the UPDATED fact. "
            "Decide whether the model answer states the updated information. Answer yes if the "
            "updated answer is present even when older information is also included; answer no "
            "only if the model answer gives the stale answer."
        ),
        "single-session-preference": (
            "This is a preference question. Decide whether the model answer correctly recalls "
            "and uses the user's personal information or preference. Do not require it to cover "
            "every rubric point; using the correct personal information is enough. Answer yes or no."
        ),
        "single-session-assistant": (
            "This is an assistant-history question. Decide whether the model answer is "
            "semantically equivalent to the reference answer; equivalent intermediate steps or "
            "phrasings count as correct. Answer yes or no."
        ),
    }
    if question_type == "abstention":
        return (
            "The reference answer states that the question is unanswerable from the "
            "conversation history. Decide whether the model answer correctly says the "
            "information is unavailable. Answer yes if it declines to answer or states the "
            "information is not present; answer no if it invents an answer."
        )
    if question_type in rubric:
        return rubric[question_type]
    return (
        "Decide whether the model answer is semantically equivalent to the reference "
        "answer. Equivalent phrasings, units and rounding count as correct. Answer yes "
        "or no."
    )


@dataclass(slots=True)
class LLMJudge:
    """Transport-injected local judge; its result is always non-official."""

    client: Any
    spec: JudgeSpec
    calls: int = 0
    last_usage: tuple[int | None, int | None] = (None, None)

    def judge(self, *, question: str, reference: str, hypothesis: str,
              question_type: str) -> JudgeVerdict:
        import json as _json
        from time import perf_counter

        self.calls += 1
        abstention = question_type == "abstention"
        instruction = _anscheck_instruction(question_type)
        prompt = _json.dumps({
            "instruction": instruction,
            "question": question,
            "reference_answer": reference if not abstention else "(the question is unanswerable)",
            "model_answer": hypothesis,
            "output": {"verdict": "yes|no"},
        }, ensure_ascii=False)
        started = perf_counter()
        try:
            completion = self.client.complete(
                model=self.spec.model,
                messages=[
                    {"role": "system", "content": "You are a strict benchmark judge. Reply with JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.spec.temperature,
                max_tokens=self.spec.max_tokens,
                timeout_seconds=self.spec.timeout_seconds,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - a judge failure must be visible, not fatal
            return JudgeVerdict(None, "", error=f"{type(exc).__name__}: {exc}",
                                latency_ms=(perf_counter() - started) * 1000)
        latency = (perf_counter() - started) * 1000
        self.last_usage = (completion.input_tokens, completion.output_tokens)
        try:
            payload = _json.loads(completion.text)
            verdict = str(payload.get("verdict", "")).strip().casefold()
        except (TypeError, ValueError, AttributeError):
            return JudgeVerdict(None, completion.text[:200], error="unparseable judge response",
                                latency_ms=latency,
                                input_tokens=completion.input_tokens,
                                output_tokens=completion.output_tokens)
        if verdict not in {"yes", "no"}:
            return JudgeVerdict(None, completion.text[:200], error=f"unexpected verdict {verdict!r}",
                                latency_ms=latency,
                                input_tokens=completion.input_tokens,
                                output_tokens=completion.output_tokens)
        return JudgeVerdict(verdict == "yes", verdict.upper(), latency_ms=latency,
                            input_tokens=completion.input_tokens,
                            output_tokens=completion.output_tokens)


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_usd_per_million: float
    output_usd_per_million: float
    source: str = "user-supplied"


def estimate_cost(pricing: ModelPricing | None, input_tokens: int | None,
                  output_tokens: int | None) -> float | None:
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * pricing.input_usd_per_million
            + output_tokens * pricing.output_usd_per_million) / 1_000_000


def load_pricing(path: str | Path | None) -> dict[str, ModelPricing]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    pricing: dict[str, ModelPricing] = {}
    for model, values in payload.items():
        pricing[model] = ModelPricing(
            float(values["input_per_million"]),
            float(values["output_per_million"]),
            str(values.get("source", "user-supplied")),
        )
    return pricing


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


def attribute(record: Mapping[str, Any], gold_turn_refs: Iterable[str],
              gold_session_ids: Iterable[str], judged_correct: bool | None,
              should_abstain: bool) -> str:
    """Say *which stage* should own a result, using the internal trace.

    The taxonomy is the one the audit report proposes:

    ``gold evidence never formed`` -> formation
    ``formed but not retrieved``   -> retrieval
    ``retrieved but judged wrong`` -> reading
    """

    formation = record.get("formation") or {}
    counts = formation.get("turn_memory_counts") or {}
    turn_events = formation.get("turn_events") or {}
    gold_turns = list(gold_turn_refs)
    if should_abstain or not gold_turns:
        # An answerable question with no gold evidence would be a dataset
        # defect; an abstention question has none by construction.
        return "CORRECT" if judged_correct else "READING"
    formed = any(int(counts.get(ref, 0)) > 0 for ref in gold_turns)
    if not formed:
        return "FORMATION"
    gold_events = {turn_events[ref] for ref in gold_turns if ref in turn_events}
    returned = (record.get("retrieval") or {}).get("returned_source_refs") or []
    retrieved = any(gold_events & set(refs or []) for refs in returned)
    if not retrieved:
        return "RETRIEVAL"
    return "CORRECT" if judged_correct else "READING"


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def latency_block(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "samples": len(values),
        "mean": sum(values) / len(values),
        "p50": median(values),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
    }


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def analyze(
    *,
    dataset_path: str | Path,
    submission_path: str | Path,
    traces_dir: str | Path | None = None,
    summary_path: str | Path | None = None,
    judge: Judge | None = None,
    reader_pricing: ModelPricing | None = None,
    judge_pricing: ModelPricing | None = None,
    examples: int = 5,
) -> dict[str, Any]:
    dataset = load_oracle_dataset(dataset_path, expected_cases=None)
    cases = dataset.by_id()
    validation = validate_submission(submission_path, expected_ids=dataset.question_ids)
    submission = read_submission(submission_path)

    hypotheses: dict[str, str] = {}
    for row in submission:
        question_id = row.get("question_id")
        if isinstance(question_id, str):
            hypotheses[question_id] = str(row.get("hypothesis", ""))

    traces: dict[str, dict[str, Any]] = {}
    if traces_dir is not None:
        for path in sorted(Path(traces_dir).glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            question_id = record.get("question_id")
            if isinstance(question_id, str):
                traces[question_id] = record

    run_summary = None
    if summary_path is not None and Path(summary_path).exists():
        run_summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))

    per_question: list[dict[str, Any]] = []
    for question_id, case in cases.items():
        trace = traces.get(question_id)
        hypothesis = hypotheses.get(question_id)
        if trace is None and hypothesis is None:
            # A question that was never run still gets the full row schema. A
            # short row would make every downstream counter either raise or --
            # worse -- silently treat the gap as an answered, non-abstained,
            # error-free case. ``abstained is None`` is what keeps it out of the
            # answered and abstention slices while ``status`` records the gap.
            per_question.append({
                "question_id": question_id,
                "question_type": case.question_type,
                "should_abstain": case.should_abstain,
                "status": NOT_RUN,
                "hypothesis": None,
                "reference": case.answer,
                "abstained": None,
                "hypothesis_origin": None,
                "reader_status": None,
                "run_recorded": False,
                "judge_outcome": NOT_RUN,
                "judge_correct": None,
                "judge_raw": None,
                "judge_error": None,
                "attribution": NOT_RUN,
                "timing_ms": None,
                "tokens": None,
                "question": case.question,
            })
            continue
        should_abstain = case.should_abstain
        judged_type = "abstention" if should_abstain else case.question_type
        verdict = None
        if judge is not None and hypothesis is not None:
            verdict = judge.judge(
                question=case.question,
                reference=case.answer,
                hypothesis=hypothesis,
                question_type=judged_type,
            )
        # ``abstained`` is decided from the submitted text alone, because the
        # abstention template is the only thing a refusal, a timeout, a
        # transport error and a malformed response have in common. It is what
        # separates "the reader refused" from "the reader answered", which is
        # the distinction the headline metric has to make explicit.
        abstained = hypothesis == CANONICAL_ABSTENTION_HYPOTHESIS
        if judge is None:
            judge_outcome = "NOT_RUN"
        elif hypothesis is None:
            judge_outcome = "NO_HYPOTHESIS"
        elif verdict is not None and verdict.correct is not None:
            judge_outcome = "VERDICT"
        else:
            judge_outcome = "NO_VERDICT"
        per_question.append({
            "question_id": question_id,
            "question_type": case.question_type,
            "should_abstain": should_abstain,
            "status": (trace or {}).get("status", "UNKNOWN"),
            "hypothesis": hypothesis,
            "reference": case.answer,
            "abstained": abstained,
            "hypothesis_origin": (trace or {}).get("hypothesis_origin"),
            "reader_status": ((trace or {}).get("reader") or {}).get("status"),
            "run_recorded": trace is not None,
            "judge_outcome": judge_outcome,
            "judge_correct": verdict.correct if verdict else None,
            "judge_raw": verdict.raw if verdict else None,
            "judge_error": verdict.error if verdict else None,
            "attribution": attribute(
                trace or {}, case.evidence_refs, case.evidence_session_ids,
                verdict.correct if verdict else None, should_abstain,
            ) if verdict is not None and verdict.correct is not None else NOT_EVALUATED,
            "timing_ms": (trace or {}).get("timing_ms"),
            "tokens": (trace or {}).get("tokens"),
            "question": case.question,
        })

    judged = [row for row in per_question if row["judge_correct"] is not None]
    correct = [row for row in judged if row["judge_correct"]]
    overall_accuracy = len(correct) / len(judged) if judged else None
    # --- denominators -------------------------------------------------------
    # Three different numbers have historically been printed as "the accuracy".
    # They are all legitimate and they are all different, so each one now
    # carries its denominator in the report instead of relying on the reader to
    # infer it. ``full_set_accuracy`` is the headline: a refusal is correct
    # only when the question is genuinely unanswerable.
    # ``is False`` / ``is True`` rather than truthiness: a question that was
    # never run carries ``abstained is None`` and belongs in neither slice. It
    # still counts against the full-set denominator, because a submission that
    # did not answer a question did not answer it.
    answered_rows = [row for row in per_question if row.get("abstained") is False]
    abstained_rows = [row for row in per_question if row.get("abstained") is True]
    not_run_rows = [row for row in per_question if row.get("abstained") is None]
    total_questions = len(per_question)
    full_set_accuracy = (len(correct) / total_questions
                         if total_questions and judge is not None else None)
    answered_correct = [row for row in answered_rows if row["judge_correct"] is True]
    answered_only_accuracy = len(answered_correct) / len(answered_rows) if answered_rows else None

    def slice_accuracy(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        evaluated = [row for row in rows if row["judge_correct"] is not None]
        hits = sum(1 for row in evaluated if row["judge_correct"])
        full_set = hits / len(rows) if rows and judge is not None else None
        judged_only = hits / len(evaluated) if evaluated else None
        return {
            "cases": len(rows),
            "evaluated": len(evaluated),
            "not_evaluated": len(rows) - len(evaluated),
            "correct": hits,
            "accuracy": full_set,
            "accuracy_full_set": full_set,
            "accuracy_judged_only": judged_only,
        }

    by_type: dict[str, dict[str, Any]] = {}
    for question_type in sorted({row["question_type"] for row in per_question}):
        by_type[question_type] = slice_accuracy(
            [row for row in per_question if row["question_type"] == question_type]
        )
    abstention_rows = [row for row in per_question if row["should_abstain"]]
    abstention_block = slice_accuracy(abstention_rows)
    # ``expected_cases`` is the published figure for the full benchmark and is
    # kept so a partial run is visibly partial; the observed count is recorded
    # next to it rather than replacing it.
    abstention_block["expected_cases"] = 30
    abstention_block["observed_abstention_questions"] = len(abstention_rows)
    type_accuracies = [block["accuracy"] for block in by_type.values()
                       if block["accuracy"] is not None]
    task_averaged = _mean(type_accuracies) if type_accuracies else None

    attribution_counts: dict[str, int] = {}
    for row in per_question:
        key = str(row["attribution"])
        attribution_counts[key] = attribution_counts.get(key, 0) + 1

    def collect(key: str) -> list[float]:
        values: list[float] = []
        for row in per_question:
            timing = row.get("timing_ms") or {}
            value = timing.get(key)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    def tokens_total(key: str) -> int | None:
        total = 0
        seen = False
        for row in per_question:
            value = (row.get("tokens") or {}).get(key)
            if isinstance(value, int):
                total += value
                seen = True
        return total if seen else None

    reader_input = tokens_total("reader_input")
    reader_output = tokens_total("reader_output")

    successes = [row for row in judged if row["judge_correct"]]
    failures = [row for row in judged if not row["judge_correct"]]

    judge_spec = getattr(judge, "spec", None)
    report: dict[str, Any] = {
        "schema_version": "dive-oracle-evaluation-v1",
        "dataset": {
            "name": dataset.declared_name,
            "path": str(dataset.path),
            "sha256": dataset.sha256,
            "cases": len(dataset.cases),
            "question_type_counts": dataset.question_type_counts,
            "abstention_cases": len(dataset.abstention_ids),
        },
        "submission": validation.to_dict(),
        "counts": {
            "questions_total": len(dataset.cases),
            "lines_in_submission": len(submission),
            "run_records": len(traces) if traces else UNKNOWN,
            "successful": (run_summary or {}).get("successful", UNKNOWN),
            "degraded": (run_summary or {}).get("degraded_cases", UNKNOWN),
            "failed": (run_summary or {}).get("failed", UNKNOWN),
            "timeouts": (run_summary or {}).get("timeouts", UNKNOWN),
            "answered": len(answered_rows),
            "abstain": len(abstained_rows),
            "not_run": len(not_run_rows),
            "judged": len(judged),
            # Counting judge errors over ``judged`` is vacuous -- that list is
            # defined by having a verdict -- so it is counted over the rows the
            # judge was actually consulted on instead.
            "judge_errors": sum(1 for row in per_question if row["judge_outcome"] == "NO_VERDICT"),
            "judge_not_consulted": sum(
                1 for row in per_question if row["judge_outcome"] in {"NOT_RUN", "NO_HYPOTHESIS"}
            ),
        },
        "accuracy": {
            "score_type": "SELF_REPORTED_NON_OFFICIAL",
            "headline_metric": "full_set_accuracy",
            "full_set_accuracy": full_set_accuracy,
            "answered_only_accuracy": answered_only_accuracy,
            "judged_accuracy": overall_accuracy,
            "abstain_rate": (len(abstained_rows) / total_questions) if total_questions else None,
            "denominators": {
                "full_set": total_questions,
                "answered_only": len(answered_rows),
                "judged": len(judged),
                "abstain": len(abstained_rows),
            },
            # Kept under its historical name so older readers do not break. Its
            # denominator is ``judged``, NOT the question count: a judge that
            # skips answers must not silently raise the score. Read
            # ``full_set_accuracy`` for the benchmark-facing number.
            "overall_accuracy": overall_accuracy,
            "task_averaged_accuracy": task_averaged,
            "by_question_type": by_type,
            "abstention": abstention_block,
            "benchmark_projection_40x": (full_set_accuracy * 40) if full_set_accuracy is not None else None,
            "benchmark_projection_40x_judged_only":
                (overall_accuracy * 40) if overall_accuracy is not None else None,
            "official_score": NOT_EVALUATED,
            "caveat": (
                "Benchmark = Overall Accuracy x 40, computed by the project group from a unified "
                "re-run of the submission with the published evaluate_qa.py and judge model. "
                "Locally, ``benchmark_projection_40x`` multiplies ``full_set_accuracy``; "
                "a correct refusal on an unanswerable question can count as correct, while "
                "an incorrect refusal on an answerable question is a miss. "
                "``overall_accuracy`` keeps its historical denominator "
                "(verdicts actually returned by the judge) and must not be quoted without it. "
                "All local results are self-reported and must not be quoted as the official "
                "Benchmark score, even if a judge spec claims to be official."
            ),
        },
        "error_attribution": {
            "taxonomy": ["CORRECT", "FORMATION", "RETRIEVAL", "READING", NOT_EVALUATED, NOT_RUN],
            "counts": attribution_counts,
            "note": (
                "FORMATION = gold evidence turn produced no memory; RETRIEVAL = a memory exists "
                "but no returned result traces back to a gold turn; READING = gold evidence was "
                "retrieved and the answer is still wrong."
            ),
        },
        "latency_ms": {
            "ingest": latency_block(collect("ingest")),
            "retrieve": latency_block(collect("retrieve")),
            "pack": latency_block(collect("pack")),
            "read": latency_block(collect("read")),
            "end_to_end": latency_block(collect("total")),
        },
        "tokens": {
            "reader_input_total": reader_input,
            "reader_output_total": reader_output,
            "judge_input_total": UNKNOWN,
            "judge_output_total": UNKNOWN,
            "judge_token_note": (
                "Per-call judge usage is not persisted onto the per-question rows, so no judge "
                "token total is claimed."
            ),
        },
        "calls": {
            "retrieval": (run_summary or {}).get("calls", {}).get("retrieval", UNKNOWN),
            "reader": (run_summary or {}).get("calls", {}).get("reader", UNKNOWN),
            "judge": getattr(judge, "calls", UNKNOWN),
            "embedding": (run_summary or {}).get("calls", {}).get("embedding", UNKNOWN),
        },
        "cost": {
            "currency": "USD",
            "reader": {
                "pricing": asdict(reader_pricing) if reader_pricing else UNKNOWN,
                "estimate_usd": estimate_cost(reader_pricing, reader_input, reader_output),
            },
            "judge": {
                "pricing": asdict(judge_pricing) if judge_pricing else UNKNOWN,
                "estimate_usd": UNKNOWN,
            },
            "total": {
                "estimate_usd": estimate_cost(reader_pricing, reader_input, reader_output),
                "status": ("PARTIAL: reader cost only; judge cost requires the project group's "
                           "judge token counts" ),
            },
            "note": (
                "No price table was supplied, so no cost is claimed."
                if reader_pricing is None else
                "Prices are caller-supplied and are not vendor-verified; the project group's "
                "judge cost is not included."
            ),
        },
        "wall_clock": {
            "run_duration_seconds": (run_summary or {}).get("duration_seconds", UNKNOWN),
            "submission_sha256": _sha256_of(submission_path),
        },
        "configuration": {
            "reader": _reader_spec_from_traces(traces),
            "judge": judge_spec.to_dict() if judge_spec else NOT_RUN,
        },
        "examples": {
            "successes": _examples(successes, examples),
            "failures": _examples(failures, examples),
        },
        "not_measured": _not_measured(judge, visited=bool(traces)),
    }
    return report


def _sha256_of(path: str | Path) -> str | None:
    try:
        from .loader import sha256_file

        return sha256_file(path)
    except OSError:
        return None


def _examples(rows: Sequence[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:max(0, limit)]:
        out.append({
            "question_id": row.get("question_id"),
            "question_type": row.get("question_type"),
            "question": row.get("question"),
            "reference_answer": row.get("reference"),
            "hypothesis": row.get("hypothesis"),
            "judge": row.get("judge_raw"),
            "attribution": row.get("attribution"),
            "reader_status": row.get("reader_status"),
        })
    return out


def _judge_token_stream() -> Iterable[int | None]:
    """Reserved hook for per-call judge usage.

    The current judges do not persist usage onto the per-question rows, so the
    report says ``UNKNOWN`` rather than inventing a number.
    """

    return []


def _reader_spec_from_traces(traces: Mapping[str, Mapping[str, Any]]) -> Any:
    for record in traces.values():
        spec = ((record.get("reader") or {}).get("spec"))
        if spec:
            return spec
    return UNKNOWN


def _not_measured(judge: Judge | None, *, visited: bool) -> dict[str, str]:
    return {
        "official_overall_accuracy": NOT_EVALUATED,
        "official_judge": NOT_EVALUATED,
        "project_group_rerun": NOT_EVALUATED,
        "judge_token_usage": UNKNOWN if judge is not None else NOT_RUN,
        "embedding_call_count": UNKNOWN if visited else NOT_RUN,
        "reason": (
            "Only the project group's unified re-run with the published evaluate_qa.py and its "
            "judge model produces the official Benchmark score."
        ),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dive_memory.oracle.evaluate",
        description="Analyse an Oracle run: accuracy, attribution, latency, token and cost.",
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--submission", required=True)
    parser.add_argument("--run-dir", help="run directory containing traces/ and summary.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--judge", choices=("exact", "openai", "none"), default="none")
    parser.add_argument("--judge-endpoint")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-api-key-env", default="DIVE_JUDGE_API_KEY")
    parser.add_argument("--judge-official", action="store_true",
                        help="unsupported legacy flag; official scores require the project-group rerun")
    parser.add_argument("--pricing-file", help="JSON: {model: {input_per_million, output_per_million}}")
    parser.add_argument("--reader-model", help="model key to price in the pricing file")
    parser.add_argument("--judge-model-key", help="judge model key to price")
    parser.add_argument("--examples", type=int, default=5)
    return parser


def build_judge(args: argparse.Namespace) -> Judge | None:
    if args.judge_official:
        raise SystemExit("--judge-official is unsupported: only the project group's rerun is official")
    if args.judge == "none":
        return None
    if args.judge == "exact":
        return NormalizedExactMatchJudge()
    import os

    from .providers import OpenAICompatibleChatClient

    if not args.judge_endpoint or not args.judge_model:
        raise SystemExit("--judge openai requires --judge-endpoint and --judge-model")
    key = os.environ.get(args.judge_api_key_env or "")
    if not key:
        raise SystemExit(f"--judge openai requires an API key in ${args.judge_api_key_env}")
    return LLMJudge(
        OpenAICompatibleChatClient(args.judge_endpoint, key, provider="openai-compatible"),
        JudgeSpec(provider="openai-compatible", model=args.judge_model, official=False),
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    pricing = load_pricing(args.pricing_file)
    run_dir = Path(args.run_dir) if args.run_dir else None
    report = analyze(
        dataset_path=args.dataset,
        submission_path=args.submission,
        traces_dir=(run_dir / "traces") if run_dir else None,
        summary_path=(run_dir / "summary.json") if run_dir else None,
        judge=build_judge(args),
        reader_pricing=pricing.get(args.reader_model or ""),
        judge_pricing=pricing.get(args.judge_model_key or ""),
        examples=max(0, args.examples),
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    print(json.dumps({
        "score_type": report["accuracy"]["score_type"],
        "full_set_accuracy": report["accuracy"]["full_set_accuracy"],
        "answered_only_accuracy": report["accuracy"]["answered_only_accuracy"],
        "abstain_rate": report["accuracy"]["abstain_rate"],
        "denominators": report["accuracy"]["denominators"],
        "overall_accuracy_judged_only": report["accuracy"]["overall_accuracy"],
        "benchmark_projection_40x": report["accuracy"]["benchmark_projection_40x"],
        "submission_status": report["submission"]["status"],
        "official_score": report["accuracy"]["official_score"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
