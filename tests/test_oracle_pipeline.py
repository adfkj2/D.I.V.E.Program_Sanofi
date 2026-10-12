"""Oracle protocol tests: loader, leakage boundary, pipeline, runner, validator, evaluator.

The handbook's Oracle requirements translate into concrete, falsifiable checks:

* the loader accepts only the published 9-field schema and the official 500
  questions (the on-disk artifact when present, a synthetic replica otherwise);
* the gold answer and its friends never reach a writer-visible surface --
  proven with a deliberate sentinel, not with a convention;
* one question cannot contaminate another (per-case namespace + store);
* the submission JSONL is exactly two fields per line, unique, complete;
* an interrupted run resumes without re-running finished questions, and a
  changed configuration is refused;
* reader timeout / error / malformed output are distinct from a real
  abstention;
* the evaluator computes accuracy from artifacts and marks what it did not
  measure as NOT_RUN / NOT_EVALUATED / UNKNOWN.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dive_memory.eval_adapters.longmemeval import (
    LongMemEvalCase,
    LongMemEvalSchemaError,
    LongMemEvalTurn,
)
from dive_memory.oracle.contracts import (
    ABSTENTION_SUFFIX,
    CANONICAL_ABSTENTION_HYPOTHESIS,
    EVALUATOR_ONLY_FIELDS,
    ORACLE_QUESTION_TYPES,
    ORACLE_REQUIRED_FIELDS,
    SUBMISSION_FIELDS,
    WRITER_VISIBLE_FIELDS,
    HypothesisOrigin,
    LeakageError,
    OracleResumeConflictError,
    ReaderStatus,
    Stage,
    assert_no_gold_in_text,
    assert_writer_visible,
    find_evaluator_only_fields,
)
from dive_memory.oracle.evaluate import (
    JudgeSpec,
    JudgeVerdict,
    ModelPricing,
    NormalizedExactMatchJudge,
    analyze,
    attribute,
    build_judge,
    build_parser as evaluate_parser,
    estimate_cost,
    latency_block,
    looks_like_refusal,
    normalize_answer,
)
from dive_memory.oracle.loader import (
    OracleLoadError,
    is_abstention,
    load_oracle_dataset,
    select_cases,
    sha256_file,
)
from dive_memory.oracle.pipeline import (
    PipelineConfig,
    ingest_case,
    ordered_turns,
    role_source_type,
    run_case,
)
from dive_memory.oracle.providers import ChatTransportError, ScriptedChatClient
from dive_memory.oracle.reader import (
    DeterministicReader,
    GroundedChatReader,
    OracleReaderRequest,
    ReaderSpec,
    ReaderPolicy,
    assert_prompt_is_clean,
    build_reader_prompt,
    invoke_reader,
    map_decision,
    reader_outcome,
)
from dive_memory.oracle.runner import (
    OracleRunner,
    atomic_write_text,
    build_parser as runner_parser,
    main as runner_main,
    trace_filename,
)
from dive_memory.oracle.validator import (
    main as validator_main,
    read_submission,
    validate_submission,
)
from dive_memory.service import MemoryService

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_ORACLE_PATH = REPO_ROOT / "eval" / "external" / "longmemeval" / "longmemeval_oracle.json"

GOLD_SENTINEL = "ZQXGOLDSENTINEL7011"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _turn(session_id, turn_index, role, content, occurred_at, *,
          has_answer=False, haystack_index=0, occurrence=1):
    return LongMemEvalTurn(
        session_id=session_id,
        session_occurrence=occurrence,
        turn_index=turn_index,
        role=role,
        content=content,
        occurred_at=occurred_at,
        has_answer=has_answer,
        haystack_index=haystack_index,
    )


def _sessions_spec(default=None):
    """[(session_id, occurred_at, [{"role", "content", "has_answer"}]), ...]"""

    if default is None:
        # "remember" is a v1 write-gate marker: without it the gate scores
        # ordinary chit-chat below the write threshold and nothing forms.
        default = [
            ("s1", "2024-05-01T10:00:00Z", [
                {"role": "user",
                 "content": "Please remember that my golden retriever is named Biscuit.",
                 "has_answer": True},
                {"role": "assistant", "content": "That is wonderful news about Biscuit."},
            ]),
        ]
    return default


def _case(question_id="q-001", *, question="What is the name of the golden retriever I adopted?",
          answer="Biscuit", question_type="single-session-user",
          question_date="2024-05-10T10:00:00Z", sessions=None):
    sessions = _sessions_spec(sessions)
    turns = []
    for session_id, occurred_at, messages in sessions:
        for index, message in enumerate(messages):
            turns.append(_turn(
                session_id, index, message["role"], message["content"], occurred_at,
                has_answer=message.get("has_answer", False),
                haystack_index=len(turns),
            ))
    evidence = tuple({session_id for session_id, _, messages in sessions
                      if any(message.get("has_answer") for message in messages)})
    return LongMemEvalCase(
        question_id=question_id,
        question=question,
        answer=answer,
        question_type=question_type,
        question_date=question_date,
        turns=tuple(turns),
        evidence_session_ids=evidence,
    )


def _oracle_row(question_id="q-001", *, question="What is the name of the golden retriever I adopted?",
                answer="Biscuit", question_type="single-session-user",
                question_date="2024-05-10T10:00:00Z", sessions=None):
    sessions = _sessions_spec(sessions)
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": question,
        "answer": answer,
        "question_date": question_date,
        "haystack_dates": [occurred_at for _, occurred_at, _ in sessions],
        "haystack_session_ids": [session_id for session_id, _, _ in sessions],
        "haystack_sessions": [messages for _, _, messages in sessions],
        "answer_session_ids": [session_id for session_id, _, messages in sessions
                                if any(message.get("has_answer") for message in messages)],
    }


def _write_dataset(tmp_path, rows, name="longmemeval_oracle.json"):
    path = tmp_path / name
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def _dataset(tmp_path, rows):
    return load_oracle_dataset(_write_dataset(tmp_path, rows), expected_cases=None)


# --------------------------------------------------------------------------- #
# Loader: schema, completeness, selection
# --------------------------------------------------------------------------- #


class TestOracleLoader:
    def test_synthetic_dataset_loads_with_unique_namespaces(self, tmp_path):
        dataset = _dataset(tmp_path, [
            _oracle_row("q-001"),
            _oracle_row("q-002", sessions=[
                ("s1", "2024-05-01T10:00:00Z", [
                    {"role": "user", "content": "My favorite color is teal."}]),
            ]),
        ])
        assert dataset.question_ids == ("q-001", "q-002")
        namespaces = {case.namespace for case in dataset.cases}
        assert namespaces == {"longmemeval:q-001", "longmemeval:q-002"}
        assert dataset.sha256 == sha256_file(dataset.path)
        assert dataset.size_bytes == dataset.path.stat().st_size

    def test_missing_required_field_rejected(self, tmp_path):
        row = _oracle_row()
        del row["question_date"]
        with pytest.raises(OracleLoadError, match="missing required Oracle fields"):
            load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None)

    def test_extra_field_rejected_because_relabelled_release(self, tmp_path):
        row = _oracle_row()
        row["gold_evidence"] = ["s1:0"]
        with pytest.raises(OracleLoadError, match="unknown fields"):
            load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None)

    def test_unknown_question_type_rejected(self, tmp_path):
        row = _oracle_row()
        row["question_type"] = "trick-question"
        with pytest.raises(OracleLoadError, match="unknown question_type"):
            load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None)

    def test_duplicate_question_ids_rejected(self, tmp_path):
        with pytest.raises(LongMemEvalSchemaError, match="unique"):
            load_oracle_dataset(
                _write_dataset(tmp_path, [_oracle_row("q-001"), _oracle_row("q-001")]),
                expected_cases=None,
            )

    def test_wrong_case_count_rejected_unless_partial_allowed(self, tmp_path):
        path = _write_dataset(tmp_path, [_oracle_row("q-001"), _oracle_row("q-002")])
        with pytest.raises(OracleLoadError, match="expected 500"):
            load_oracle_dataset(path)
        dataset = load_oracle_dataset(path, expected_cases=None)
        with pytest.raises(OracleLoadError, match="partial file must not be reported"):
            dataset.require_full_benchmark()
        assert len(dataset.cases) == 2

    def test_future_session_rejected_at_load(self, tmp_path):
        row = _oracle_row(sessions=[
            ("s1", "2024-06-01T10:00:00Z", [
                {"role": "user", "content": "I moved to Oslo in June."}]),
        ])
        with pytest.raises(LongMemEvalSchemaError, match="leaks past cutoff"):
            load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None)

    def test_slash_dates_use_calendar_day_but_reject_next_day(self, tmp_path):
        row = _oracle_row(
            question_date="2024/05/01 (Wed) 06:47",
            sessions=[("s1", "2024/05/01 (Wed) 21:04", [
                {"role": "user", "content": "Please remember that my dog is named Biscuit.",
                 "has_answer": True},
            ])],
        )
        case = load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None).cases[0]
        assert case.visibility_cutoff_iso == "2024-05-01T23:59:59.999999+00:00"
        result = run_case(case, reader=DeterministicReader())
        assert result.record["retrieval"]["returned"] > 0
        assert result.record["chronology"]["turns_after_question_date"] == []
        assert result.record["evidence"]["temporal_scaffold"]["question_date"] == \
            case.visibility_cutoff_iso
        assert all(view["days_before_question"] >= 0
                   for view in result.record["evidence"]["views"]
                   if view["days_before_question"] is not None)

        row["haystack_dates"] = ["2024/05/02 (Thu) 00:01"]
        with pytest.raises(LongMemEvalSchemaError, match="leaks past cutoff"):
            load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None)

    def test_iso_dates_keep_instant_precision(self, tmp_path):
        row = _oracle_row(
            question_date="2024-05-01T06:47:00Z",
            sessions=[("s1", "2024-05-01T21:04:00Z", [
                {"role": "user", "content": "Please remember my dog's name."},
            ])],
        )
        with pytest.raises(LongMemEvalSchemaError, match="leaks past cutoff"):
            load_oracle_dataset(_write_dataset(tmp_path, [row]), expected_cases=None)

    def test_jsonl_dataset_also_loads(self, tmp_path):
        path = tmp_path / "partial.jsonl"
        path.write_text(json.dumps(_oracle_row("q-001")) + "\n"
                        + json.dumps(_oracle_row("q-002")) + "\n", encoding="utf-8")
        dataset = load_oracle_dataset(path, expected_cases=None)
        assert dataset.question_ids == ("q-001", "q-002")

    def test_is_abstention(self):
        assert is_abstention("q-100_abs")
        assert is_abstention("Q-100_ABS")
        assert not is_abstention("q-100")

    def test_select_cases_rejects_unknown_ids(self):
        with pytest.raises(OracleLoadError, match="unknown question ids"):
            select_cases([_case("q-001")], question_ids=["nope"])

    def test_select_cases_explicit_order_preserved(self):
        selected = select_cases([_case("q-001"), _case("q-002"), _case("q-003")],
                                 question_ids=["q-003", "q-001"])
        assert [case.question_id for case in selected] == ["q-003", "q-001"]

    def test_select_cases_max_cases_head_slice(self):
        selected = select_cases([_case(f"q-{i:03d}") for i in range(5)], max_cases=2)
        assert [case.question_id for case in selected] == ["q-000", "q-001"]

    def test_stratified_selection_deterministic_and_typed(self):
        cases = []
        for index in range(60):
            question_type = list(ORACLE_QUESTION_TYPES)[index % len(ORACLE_QUESTION_TYPES)]
            cases.append(_case(f"q-{index:03d}", question_type=question_type))
        first = select_cases(cases, stratified_size=12, seed=7)
        second = select_cases(cases, stratified_size=12, seed=7)
        assert [case.question_id for case in first] == [case.question_id for case in second]
        assert len(first) == 12
        assert {case.question_type for case in first} <= ORACLE_QUESTION_TYPES
        other = select_cases(cases, stratified_size=12, seed=8)
        assert [case.question_id for case in other] != [case.question_id for case in first]

    @pytest.mark.skipif(not REAL_ORACLE_PATH.exists(),
                        reason="official longmemeval_oracle.json is not on this machine")
    def test_real_oracle_dataset_is_500_unique_ids_and_30_abstentions(self):
        dataset = load_oracle_dataset(REAL_ORACLE_PATH)
        assert len(dataset.cases) == 500
        assert len(set(dataset.question_ids)) == 500
        assert len(dataset.abstention_ids) == 30
        assert all(question_id.endswith(ABSTENTION_SUFFIX)
                   for question_id in dataset.abstention_ids)
        assert set(dataset.question_type_counts) <= ORACLE_QUESTION_TYPES
        assert sum(dataset.question_type_counts.values()) == 500


# --------------------------------------------------------------------------- #
# Leakage boundary (sentinel-based, not convention-based)
# --------------------------------------------------------------------------- #


class TestLeakageBoundary:
    def test_system_input_is_exactly_the_writer_allow_list(self):
        rows = list(_case().system_input())
        assert rows, "a case with turns must yield writer rows"
        assert_writer_visible(rows)
        for row in rows:
            assert set(row) == WRITER_VISIBLE_FIELDS

    def test_assert_writer_visible_rejects_gold_carrying_row(self):
        rows = [{"namespace": "x", "session_id": "s", "turn_id": "s:0", "role": "user",
                 "content": "hello", "occurred_at": "2024-05-01T10:00:00Z", "answer": "Biscuit"}]
        with pytest.raises(LeakageError, match="evaluator-only fields"):
            assert_writer_visible(rows)

    def test_assert_writer_visible_rejects_extra_key(self):
        rows = [{"namespace": "x", "session_id": "s", "turn_id": "s:0", "role": "user",
                 "content": "hello", "occurred_at": "2024-05-01T10:00:00Z", "extra": 1}]
        with pytest.raises(LeakageError, match="allow-list"):
            assert_writer_visible(rows)

    def test_find_evaluator_only_fields_detects_nested_gold(self):
        payload = {"trace": {"rows": [{"turn": {"answer": "B", "question_type": "x"}}]}}
        leaked = find_evaluator_only_fields(payload)
        assert set(leaked) == {
            "trace.rows[0].turn.answer", "trace.rows[0].turn.question_type",
        }

    def test_assert_prompt_is_clean_rejects_extra_key(self):
        prompt = build_reader_prompt(OracleReaderRequest(
            question="q", question_date=None, as_of=None, items=(), context_text="c",
        ))
        assert_prompt_is_clean(prompt)
        prompt["answer"] = "nope"
        with pytest.raises(LeakageError, match="non-allow-listed keys"):
            assert_prompt_is_clean(prompt)

    def test_assert_no_gold_in_text(self):
        assert_no_gold_in_text("plain text", [GOLD_SENTINEL])
        with pytest.raises(LeakageError, match="gold sentinel"):
            assert_no_gold_in_text(f"prefix {GOLD_SENTINEL} suffix", [GOLD_SENTINEL])

    def test_end_to_end_trace_and_submission_carry_no_gold_sentinel(self, tmp_path):
        """The gold answer exists only on the case; nothing downstream may see it."""
        case = _case("q-leak", answer=GOLD_SENTINEL)
        result = run_case(case, reader=DeterministicReader())
        record = result.record
        serialized = json.dumps(record, ensure_ascii=False, default=str)
        assert GOLD_SENTINEL not in serialized
        assert not find_evaluator_only_fields(record)
        assert result.hypothesis != GOLD_SENTINEL
        # The prompt surface: rebuilding it from the trace must stay clean too.
        prompt = build_reader_prompt(OracleReaderRequest(
            question=case.question,
            question_date=case.question_date_iso,
            as_of=case.question_date_iso,
            items=(),
            context_text=str(record.get("context_text") or ""),
        ))
        assert_prompt_is_clean(prompt)
        assert GOLD_SENTINEL not in json.dumps(prompt, ensure_ascii=False)

    def test_submission_lines_carry_exactly_two_fields(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001"), _oracle_row("q-002")])
        runner = OracleRunner(dataset=dataset, output_dir=tmp_path / "run",
                              reader=DeterministicReader())
        runner.run()
        lines = read_submission(tmp_path / "run" / "submission.jsonl")
        assert lines
        for row in lines:
            assert set(row) == SUBMISSION_FIELDS


# --------------------------------------------------------------------------- #
# Pipeline: chronology, cutoff, isolation, statuses
# --------------------------------------------------------------------------- #


class TestPipeline:
    def test_run_case_completes_and_forms_memories(self):
        result = run_case(_case(), reader=DeterministicReader())
        record = result.record
        assert result.ok
        assert record["status"] == "COMPLETED"
        assert record["failure"] is None
        assert record["formation"]["memories_formed"] > 0
        assert record["hypothesis_origin"] in {HypothesisOrigin.READER.value,
                                              HypothesisOrigin.READER_ABSTENTION.value}
        assert isinstance(result.hypothesis, str) and result.hypothesis
        assert record["question_id"] == "q-001"
        assert record["namespace"] == "longmemeval:q-001"
        for key in ("ingest", "retrieve", "pack", "read", "total"):
            assert isinstance(record["timing_ms"][key], float)

    def test_chronological_ingest_orders_sessions_by_real_time(self):
        # Published order: s2 (May 3) before s1 (May 1) -- an inversion the
        # official release really contains 34 times.
        case = _case(sessions=[
            ("s2", "2024-05-03T10:00:00Z", [
                {"role": "user", "content": "I now prefer swimming to running."}]),
            ("s1", "2024-05-01T10:00:00Z", [
                {"role": "user", "content": "I adopted a golden retriever named Biscuit.",
                 "has_answer": True}]),
        ])
        assert [turn.session_id for turn in case.turns] == ["s2", "s1"]
        assert [turn.session_id for turn in case.chronological_turns] == ["s1", "s2"]
        assert not case.haystack_is_chronological
        result = run_case(case, reader=DeterministicReader())
        chronology = result.record["chronology"]
        assert chronology["haystack_was_chronological"] is False
        assert chronology["first_occurred_at"].startswith("2024-05-01")
        assert chronology["last_occurred_at"].startswith("2024-05-03")
        assert chronology["turns_after_question_date"] == []

    def test_ordered_turns_can_fall_back_to_published_order(self):
        case = _case(sessions=[
            ("s2", "2024-05-03T10:00:00Z", [{"role": "user", "content": "later"}]),
            ("s1", "2024-05-01T10:00:00Z", [{"role": "user", "content": "earlier"}]),
        ])
        assert [t.session_id for t in ordered_turns(case, enforce=False)] == ["s2", "s1"]

    def test_pipeline_reports_turns_past_question_date(self):
        # Hand-built cases bypass the loader, so the pipeline's own cutoff
        # accounting must still surface a future turn (the loader rejects such
        # files outright; see the loader tests).
        case = _case(question_date="2024-05-02T00:00:00Z", sessions=[
            ("s1", "2024-05-01T10:00:00Z", [
                {"role": "user", "content": "I adopted a golden retriever named Biscuit.",
                 "has_answer": True}]),
            ("s2", "2024-05-05T10:00:00Z", [
                {"role": "user", "content": "future turn"}]),
        ])
        result = run_case(case, reader=DeterministicReader())
        rejected = result.record["chronology"]["turns_after_question_date"]
        assert rejected == ["s2:0"]
        assert result.record["formation"]["turns_ingested"] == 1

    def test_role_source_type(self):
        assert role_source_type("user") == "user"
        assert role_source_type("human") == "user"
        assert role_source_type("assistant") == "assistant"
        assert role_source_type("ASSISTANT") == "assistant"

    def test_namespace_isolation_between_cases(self):
        service = MemoryService()
        try:
            case_a = _case("q-a")
            case_b = _case("q-b")
            ingest_case(service, case_a, turns=case_a.turns)
            ingest_case(service, case_b, turns=case_b.turns)
            result_a = service.retrieve(case_a.namespace, "golden retriever", limit=5)
            result_b = service.retrieve(case_b.namespace, "golden retriever", limit=5)
            assert result_a.items or result_b.items
            for item in result_a.items:
                assert item.memory.namespace == case_a.namespace
            for item in result_b.items:
                assert item.memory.namespace == case_b.namespace
        finally:
            service.store.close()

    def test_per_case_service_is_structurally_disposable(self):
        # run_case builds its own store; two runs cannot share state.
        first = run_case(_case("q-a"), reader=DeterministicReader())
        second = run_case(_case("q-b"), reader=DeterministicReader())
        assert first.record["namespace"] != second.record["namespace"]

    def test_empty_history_abstains_without_becoming_a_failure(self):
        case = _case("q-empty", sessions=[
            ("s1", "2024-05-01T10:00:00Z", [
                {"role": "user", "content": "   "},
                {"role": "assistant", "content": ""}]),
        ])
        result = run_case(case, reader=DeterministicReader())
        record = result.record
        assert record["status"] == "COMPLETED"
        assert record["failure"] is None
        assert record["retrieval"]["empty"] is True
        assert record["hypothesis_origin"] == HypothesisOrigin.READER_ABSTENTION.value
        assert result.hypothesis == CANONICAL_ABSTENTION_HYPOTHESIS
        assert record["reader"]["reason_code"] == "NO_RETRIEVAL_CANDIDATES"

    def test_summarize_record_projection(self):
        result = run_case(_case("q-001"), reader=DeterministicReader())
        projection = result.record if isinstance(result.record, dict) else None
        from dive_memory.oracle.pipeline import summarize_record
        summary = summarize_record(result.record)
        assert summary["question_id"] == "q-001"
        assert summary["status"] == "COMPLETED"
        assert summary["retrieved"] == (result.record["retrieval"]["returned"])
        assert set(summary) == {
            "question_id", "status", "failure_stage", "hypothesis", "hypothesis_origin",
            "reader_status", "reader_reason", "evidence_state", "retrieved", "context_tokens",
            "timing_ms", "tokens",
        }


# --------------------------------------------------------------------------- #
# Reader contract: answer, abstention, timeout, error, malformed output
# --------------------------------------------------------------------------- #


def _scripted_reader(responses, **spec_kwargs):
    client = ScriptedChatClient(responses=list(responses))
    spec = ReaderSpec(provider="scripted", model="scripted-chat-v1", **spec_kwargs)
    return GroundedChatReader(client, spec), client


class TestReaderContract:
    def test_deterministic_reader_answers_from_top_ranked_memory(self):
        reader = DeterministicReader()
        case = _case()
        result = run_case(case, reader=reader)
        if result.record["retrieval"]["items" if False else "returned"] > 0:
            assert result.record["reader"]["status"] == ReaderStatus.ANSWER.value
            assert result.record["hypothesis_origin"] == HypothesisOrigin.READER.value
        else:
            assert result.record["reader"]["status"] == ReaderStatus.ABSTAIN.value

    def test_reader_timeout_is_distinct_from_abstention(self):
        reader = DeterministicReader(force_failure="timeout")
        result = run_case(_case(), reader=reader)
        record = result.record
        assert record["status"] == "DEGRADED"
        assert record["reader"]["status"] == ReaderStatus.TIMEOUT.value
        assert record["reader"]["error_type"] == "READER_TIMEOUT"
        assert record["failure"]["stage"] == Stage.READING.value
        assert not result.ok
        # The submitted text is the template, but the trace keeps the truth.
        assert record["hypothesis"] is None

    def test_reader_error_is_distinct_from_abstention(self):
        reader = DeterministicReader(force_failure="error")
        record = run_case(_case(), reader=reader).record
        assert record["reader"]["status"] == ReaderStatus.ERROR.value
        assert record["reader"]["error_type"] == "READER_FAILURE"
        assert record["failure"]["stage"] == Stage.READING.value

    def test_malformed_json_maps_to_invalid_not_abstain(self):
        reader, _ = _scripted_reader(["this is not json {{{"] * 2)
        case = _case()
        result = run_case(case, reader=reader)
        record = result.record
        assert record["reader"]["status"] == ReaderStatus.INVALID.value
        assert record["reader"]["error_type"] == "MALFORMED_READER_OUTPUT"
        assert record["reader"]["calls"] == 2
        assert len(record["reader"]["attempt_errors"]) == 2
        assert record["status"] == "DEGRADED"

    def test_uncanonical_json_answer_is_rejected(self):
        # A JSON payload that does not follow the grounded-answer schema must
        # not be accepted as an answer.
        reader, _ = _scripted_reader([json.dumps({"status": "ANSWER", "answer": "Biscuit"})] * 2)
        record = run_case(_case(), reader=reader).record
        assert record["reader"]["status"] in {ReaderStatus.INVALID.value,
                                              ReaderStatus.ABSTAIN.value}
        assert record["hypothesis_origin"] != HypothesisOrigin.READER.value

    def test_format_retry_transport_failure_preserves_first_error(self):
        reader, _ = _scripted_reader(["not JSON"])
        record = run_case(_case(), reader=reader).record
        assert record["reader"]["status"] == ReaderStatus.ERROR.value
        assert record["reader"]["calls"] == 2
        assert record["reader"]["attempt_errors"][0]["reason_code"] == \
            "UNSUPPORTED_READER_OUTPUT"
        assert record["reader"]["attempt_errors"][1]["reason_code"] == \
            "READER_RETRY_FAILURE"
        assert record["reader"]["usage"]["source"].endswith("observed_only")

    def test_scripted_client_without_response_raises_transport_error(self):
        client = ScriptedChatClient(responses=[])
        with pytest.raises(ChatTransportError):
            client.complete(model="m", messages=[{"role": "user", "content": "hi"}],
                           temperature=0.0, max_tokens=8, timeout_seconds=5.0)

    def test_reader_outcome_accepts_plain_grounded_readers(self):
        class _PlainReader:
            policy_version = "grounded-answer-v1"
            accepts_empty_evidence = False

            def answer(self, question, items, *, as_of=None):
                from dive_memory.answering import AnswerStatus, EvidenceCitation, GroundedAnswerDecision
                top = items[0]
                return GroundedAnswerDecision(
                    AnswerStatus.ANSWER, top.memory.content, "PLAIN",
                    (EvidenceCitation(top.memory.id, top.memory.content,
                                      tuple(top.source_refs)),),
                )

        case = _case()
        outcome = None
        service = MemoryService()
        try:
            from dive_memory.retrieval import RetrievalConfig
            ingest_case(service, case, turns=case.turns)
            result = service.retrieve(case.namespace, case.question, limit=5,
                                      as_of=case.question_date_iso,
                                      config=RetrievalConfig.full())
            outcome = reader_outcome(_PlainReader(), OracleReaderRequest(
                question=case.question, question_date=case.question_date_iso,
                as_of=case.question_date_iso, items=tuple(result.items),
                context_text=" ".join(item.memory.content for item in result.items),
            ))
        finally:
            service.store.close()
        assert outcome.status is ReaderStatus.ANSWER
        assert outcome.origin is HypothesisOrigin.READER
        assert outcome.usage.source == "unknown"

    def test_invoke_reader_with_empty_evidence_abstains(self):
        decision = invoke_reader(DeterministicReader(), "any question", [])
        assert decision.reason_code == "NO_RETRIEVAL_CANDIDATES"

    def test_reader_spec_fingerprint_is_stable_and_typed(self):
        spec = ReaderSpec(provider="p", model="m")
        assert spec.fingerprint() == ReaderSpec(provider="p", model="m").fingerprint()
        assert spec.fingerprint() != ReaderSpec(provider="p", model="m2").fingerprint()
        assert spec.to_dict()["temperature"] == 0.0
        with pytest.raises(ValueError):
            ReaderSpec(provider="", model="m")
        with pytest.raises(ValueError):
            ReaderSpec(provider="p", model="m", max_tokens=0)
        with pytest.raises(ValueError):
            ReaderSpec(provider="p", model="m", timeout_seconds=0)

    def test_map_decision_error_reason_becomes_timeout_status(self):
        from dive_memory.answering import AnswerStatus, GroundedAnswerDecision
        decision = GroundedAnswerDecision(AnswerStatus.ERROR, None, "READER_TIMEOUT")
        outcome = map_decision(decision, usage=None, latency_ms=1.0)
        assert outcome.status is ReaderStatus.TIMEOUT
        assert outcome.resolved_hypothesis == CANONICAL_ABSTENTION_HYPOTHESIS


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #


class TestValidator:
    def _write(self, tmp_path, rows, name="submission.jsonl"):
        path = tmp_path / name
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
        return path

    def test_valid_submission(self, tmp_path):
        path = self._write(tmp_path, [
            {"question_id": "q-001", "hypothesis": "Biscuit"},
            {"question_id": "q-002", "hypothesis": CANONICAL_ABSTENTION_HYPOTHESIS},
        ])
        report = validate_submission(path, expected_ids=["q-001", "q-002"])
        assert report.status == "VALID"
        assert report.coverage == 1.0
        assert report.missing_ids == ()
        assert report.unexpected_ids == ()

    def test_duplicate_missing_unexpected_empty(self, tmp_path):
        path = self._write(tmp_path, [
            {"question_id": "q-001", "hypothesis": "Biscuit"},
            {"question_id": "q-001", "hypothesis": "Biscuit"},
            {"question_id": "q-002", "hypothesis": "   "},
            {"question_id": "q-999", "hypothesis": "nope"},
        ])
        report = validate_submission(path, expected_ids=["q-001", "q-002", "q-003"])
        assert report.status == "INVALID"
        assert report.duplicate_ids == ("q-001",)
        assert report.missing_ids == ("q-003",)
        assert report.unexpected_ids == ("q-999",)
        assert report.empty_hypotheses == ("q-002",)
        assert 0 < report.coverage < 1

    def test_extra_field_and_non_string_rejected(self, tmp_path):
        path = self._write(tmp_path, [
            {"question_id": "q-001", "hypothesis": "B", "score": 1},
            {"question_id": 42, "hypothesis": "B"},
        ])
        report = validate_submission(path)
        assert report.field_violations
        assert report.non_string_fields

    def test_malformed_line_counted_not_fatal(self, tmp_path):
        path = tmp_path / "submission.jsonl"
        path.write_text('{"question_id": "q-001", "hypothesis": "B"}\n'
                         "not json at all\n"
                         "\n", encoding="utf-8")
        report = validate_submission(path)
        assert report.malformed_lines
        assert report.blank_lines == 1
        assert report.parsed_lines == 1

    def test_require_full_expects_500(self, tmp_path):
        path = self._write(tmp_path, [{"question_id": "q-001", "hypothesis": "B"}])
        report = validate_submission(path, require_full=True)
        assert report.expected_count == 500
        assert report.coverage == 1 / 500

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            validate_submission(tmp_path / "nope.jsonl")

    def test_cli_exits_nonzero_on_invalid_submission(self, tmp_path, capsys):
        path = self._write(tmp_path, [{"question_id": "q-001", "hypothesis": ""}])
        with pytest.raises(SystemExit) as excinfo:
            validator_main([str(path), "--output", str(tmp_path / "report.json")])
        assert excinfo.value.code == 1
        payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert payload["status"] == "INVALID"
        assert payload["schema_version"] == "dive-oracle-submission-validation-v1"


# --------------------------------------------------------------------------- #
# Runner: durability, resume, manifest, failure policy
# --------------------------------------------------------------------------- #


class TestRunner:
    def _runner(self, dataset, output_dir, **kwargs):
        defaults = dict(reader=DeterministicReader())
        defaults.update(kwargs)
        return OracleRunner(dataset=dataset, output_dir=output_dir, **defaults)

    def test_full_small_run_writes_every_artifact(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row(f"q-{i:03d}") for i in range(3)])
        output = tmp_path / "run"
        summary = self._runner(dataset, output).run()

        assert summary["status"] == "COMPLETED"
        assert summary["cases_selected"] == 3
        assert summary["successful_cases"] == 3
        assert summary["submission"]["lines"] == 3
        assert summary["submission"]["expected_lines"] == 3
        assert summary["submission"]["error_fallback_lines"] == 0
        assert summary["self_reported_accuracy"] == "NOT_EVALUATED"
        assert summary["official_score"] == "NOT_EVALUATED"

        traces = list((output / "traces").glob("*.json"))
        assert len(traces) == 3
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["dataset"]["sha256"] == dataset.sha256
        assert manifest["dataset"]["cases"] == 3
        assert manifest["resume_key"]["dataset_sha256"] == dataset.sha256
        assert manifest["status"] == "COMPLETED"
        assert manifest["finished_at"] is not None
        assert manifest["duration_seconds"] >= 0
        assert manifest["artifacts"]["submission_jsonl"]["lines"] == 3
        assert manifest["artifacts"]["submission_jsonl"]["sha256"] == \
            sha256_file(output / "submission.jsonl")
        config = manifest["configuration"]
        assert config["reader_policy"] == ReaderPolicy.evidence_first().to_dict()
        assert config["scoring_policy"]["name"] == "legacy"
        assert config["scoring_policy_fingerprint"]
        assert manifest["known_limitations"]
        cases = (output / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(cases) == 3
        failures = json.loads((output / "failures.json").read_text(encoding="utf-8"))
        assert failures == []
        assert json.loads((output / "summary.json").read_text(encoding="utf-8"))["run_id"] \
            == summary["run_id"]

    def test_run_is_reproducible_across_directories(self, tmp_path):
        rows = [_oracle_row(f"q-{i:03d}") for i in range(2)]
        dataset_path = _write_dataset(tmp_path, rows)
        dataset_a = load_oracle_dataset(dataset_path, expected_cases=None)
        dataset_b = load_oracle_dataset(dataset_path, expected_cases=None)
        first = self._runner(dataset_a, tmp_path / "run-a").run()
        second = self._runner(dataset_b, tmp_path / "run-b").run()
        assert first["submission"]["sha256"] == second["submission"]["sha256"]
        assert first["submission"]["lines"] == second["submission"]["lines"]

    def test_resume_reruns_nothing_when_complete(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row(f"q-{i:03d}") for i in range(2)])
        output = tmp_path / "run"
        first = self._runner(dataset, output).run()
        second = self._runner(dataset, output).run(resume=True)
        assert second["cases_skipped_from_checkpoint"] == 2
        assert second["submission"]["sha256"] == first["submission"]["sha256"]

    @pytest.mark.parametrize("missing_trace", [False, True])
    def test_resume_preserves_checkpointed_failures_and_partial_status(self, tmp_path, missing_trace):
        dataset = _dataset(tmp_path, [_oracle_row(f"q-{i:03d}") for i in range(2)])
        output = tmp_path / "run"
        first = self._runner(dataset, output,
                             reader=DeterministicReader(force_failure="error")).run()
        if missing_trace:
            (output / "traces" / trace_filename("q-001")).unlink()
        resumed = self._runner(dataset, output,
                               reader=DeterministicReader(force_failure="error")).run(resume=True)
        assert resumed["status"] == "PARTIAL"
        assert resumed["degraded_cases"] == 2
        assert resumed["cases_skipped_from_checkpoint"] == (1 if missing_trace else 2)
        assert resumed["submission"]["sha256"] == first["submission"]["sha256"]
        failures = json.loads((output / "failures.json").read_text(encoding="utf-8"))
        assert {row["question_id"] for row in failures} == {"q-000", "q-001"}
        assert len(resumed["failures"]) == 2
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "PARTIAL"

    def test_interrupted_run_resumes_only_the_missing_question(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row(f"q-{i:03d}") for i in range(3)])
        output = tmp_path / "run"
        self._runner(dataset, output).run()
        # Simulate an interruption: one trace is lost, the rest survive.
        (output / "traces" / trace_filename("q-001")).unlink()
        summary = self._runner(dataset, output).run(resume=True)
        assert summary["cases_skipped_from_checkpoint"] == 2
        assert summary["cases_recorded"] == 3
        assert summary["submission"]["lines"] == 3
        report = validate_submission(output / "submission.jsonl",
                                     expected_ids=dataset.question_ids)
        assert report.status == "VALID"

    def test_second_run_without_resume_is_refused(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        output = tmp_path / "run"
        self._runner(dataset, output).run()
        with pytest.raises(OracleResumeConflictError, match="pass --resume"):
            self._runner(dataset, output).run()

    def test_resume_with_changed_configuration_is_refused(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        output = tmp_path / "run"
        self._runner(dataset, output).run()
        changed = self._runner(dataset, output,
                               pipeline_config=PipelineConfig(limit=5))
        with pytest.raises(OracleResumeConflictError, match="differs from the artefact"):
            changed.run(resume=True)

    def test_resume_with_changed_reader_is_refused(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        output = tmp_path / "run"
        self._runner(dataset, output, reader=DeterministicReader()).run()
        other = self._runner(dataset, output, reader=DeterministicReader(
            spec=ReaderSpec(provider="deterministic", model="another-model")))
        with pytest.raises(OracleResumeConflictError, match="differs from the artefact"):
            other.run(resume=True)

    def test_unsafe_document_scoring_pair_is_rejected(self, tmp_path):
        from types import SimpleNamespace

        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        gate = SimpleNamespace(document_admission=SimpleNamespace(mode="all"))
        with pytest.raises(ValueError, match="crowd out short evidence"):
            self._runner(dataset, tmp_path / "run", gate=gate)

    def test_traces_without_manifest_are_refused(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        output = tmp_path / "run"
        (output / "traces").mkdir(parents=True)
        (output / "traces" / "orphan.json").write_text("{}", encoding="utf-8")
        with pytest.raises(OracleResumeConflictError, match="no manifest"):
            self._runner(dataset, output).run()

    def test_force_restarts_in_place(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        output = tmp_path / "run"
        self._runner(dataset, output).run()
        summary = self._runner(dataset, output).run(force=True)
        assert summary["cases_skipped_from_checkpoint"] == 0
        assert summary["submission"]["lines"] == 1

    def test_on_error_abstain_writes_canonical_template(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row(f"q-{i:03d}") for i in range(2)])
        output = tmp_path / "run"
        summary = self._runner(dataset, output,
                               reader=DeterministicReader(force_failure="error")).run()
        # Every question was processed but every answer is an error fallback:
        # the honest status is PARTIAL, never COMPLETED.
        assert summary["status"] == "PARTIAL"
        assert summary["degraded_cases"] == 2
        assert summary["submission"]["lines"] == 2
        assert summary["submission"]["error_fallback_lines"] == 2
        for row in read_submission(output / "submission.jsonl"):
            assert row["hypothesis"] == CANONICAL_ABSTENTION_HYPOTHESIS
        failures = json.loads((output / "failures.json").read_text(encoding="utf-8"))
        assert len(failures) == 2
        assert {failure["stage"] for failure in failures} == {"READING"}
        assert all(failure["error_type"] for failure in failures)

    def test_on_error_skip_omits_failed_questions(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row(f"q-{i:03d}") for i in range(3)])
        output = tmp_path / "run"
        summary = self._runner(
            dataset, output,
            reader=DeterministicReader(force_failure="timeout"),
            on_error="skip",
        ).run()
        assert summary["submission"]["lines"] == 0
        assert summary["timeouts"] == 3

    def test_invalid_on_error_policy_rejected(self, tmp_path):
        dataset = _dataset(tmp_path, [_oracle_row("q-001")])
        with pytest.raises(ValueError):
            self._runner(dataset, tmp_path / "run", on_error="explode").run()

    def test_trace_filename_is_windows_safe_and_deterministic(self):
        assert trace_filename("q-001") == "q-001.json"
        hostile = 'q/../\\:*?"<>|'
        name = trace_filename(hostile)
        assert "/" not in name and "\\" not in name and ":" not in name
        assert trace_filename(hostile) == name

    def test_atomic_write_text_returns_sha_and_is_readable(self, tmp_path):
        path = tmp_path / "nested" / "file.txt"
        digest = atomic_write_text(path, "hello")
        assert path.read_text(encoding="utf-8") == "hello"
        assert digest == sha256_file(path)
        assert not list(tmp_path.rglob("*.tmp"))

    def test_cli_end_to_end_with_validator(self, tmp_path, capsys):
        rows = [_oracle_row(f"q-{i:03d}") for i in range(2)]
        dataset_path = _write_dataset(tmp_path, rows)
        output = tmp_path / "cli-run"
        runner_main(["--dataset", str(dataset_path), "--output", str(output),
                     "--max-cases", "1", "--allow-partial"])
        captured = json.loads(capsys.readouterr().out)
        assert captured["submission_lines"] == 1
        assert captured["overall_accuracy"] == "NOT_EVALUATED"
        report = validate_submission(output / "submission.jsonl",
                                    expected_ids=["q-000", "q-001"])
        # A partial run is a legal artifact, but against the full id set the
        # validator must report the missing question rather than bless it.
        assert report.status == "INVALID"
        assert report.missing_ids == ("q-001",)
        partial = validate_submission(output / "submission.jsonl")
        assert partial.status == "VALID"
        # The validator CLI must also flag the missing question and exit 1.
        with pytest.raises(SystemExit) as excinfo:
            validator_main([str(output / "submission.jsonl"), "--dataset", str(dataset_path)])
        assert excinfo.value.code == 1

    def test_cli_parser_requires_dataset_and_output(self):
        parser = runner_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# --------------------------------------------------------------------------- #
# Evaluator: accuracy, attribution, latency, cost, unknowns
# --------------------------------------------------------------------------- #


class TestEvaluate:
    def _evaluate_dataset(self, tmp_path, rows=None):
        rows = rows or [
            _oracle_row("q-000"),
            _oracle_row("q-001", question_type="multi-session", answer="teal", sessions=[
                ("s1", "2024-05-01T10:00:00Z", [
                    {"role": "user", "content": "I always say my favorite color is teal.",
                     "has_answer": True}]),
            ]),
            _oracle_row("q-002_abs", question_type="single-session-user",
                        answer="Unknown", sessions=[
                ("s1", "2024-05-01T10:00:00Z", [
                    {"role": "user", "content": "What is my passport number?"}]),
            ]),
        ]
        return _write_dataset(tmp_path, rows)

    def test_analyze_computes_self_reported_accuracy(self, tmp_path):
        dataset_path = self._evaluate_dataset(tmp_path)
        submission = tmp_path / "submission.jsonl"
        submission.write_text(
            json.dumps({"question_id": "q-000", "hypothesis": "Biscuit"}) + "\n"
            + json.dumps({"question_id": "q-001", "hypothesis": "teal"}) + "\n"
            + json.dumps({"question_id": "q-002_abs",
                          "hypothesis": CANONICAL_ABSTENTION_HYPOTHESIS}) + "\n",
            encoding="utf-8")
        report = analyze(dataset_path=dataset_path, submission_path=submission,
                         judge=NormalizedExactMatchJudge())
        accuracy = report["accuracy"]
        assert accuracy["score_type"] == "SELF_REPORTED_NON_OFFICIAL"
        assert accuracy["overall_accuracy"] == pytest.approx(1.0)
        assert accuracy["benchmark_projection_40x"] == pytest.approx(40.0)
        assert accuracy["abstention"]["correct"] == 1
        assert accuracy["abstention"]["expected_cases"] == 30
        assert report["counts"]["questions_total"] == 3
        assert report["counts"]["judged"] == 3
        assert set(accuracy["by_question_type"]) <= ORACLE_QUESTION_TYPES

    def test_analyze_marks_wrong_answers_and_missing_questions(self, tmp_path):
        dataset_path = self._evaluate_dataset(tmp_path)
        submission = tmp_path / "submission.jsonl"
        submission.write_text(
            json.dumps({"question_id": "q-000", "hypothesis": "wrong"}) + "\n"
            + json.dumps({"question_id": "q-002_abs", "hypothesis": "Paris"}) + "\n",
            encoding="utf-8")
        report = analyze(dataset_path=dataset_path, submission_path=submission,
                         judge=NormalizedExactMatchJudge())
        assert report["accuracy"]["overall_accuracy"] == pytest.approx(0.0)
        assert report["accuracy"]["official_score"] == "NOT_EVALUATED"
        # q-001 was never run: no traces were supplied, so the report must not
        # claim a run record count it cannot know.
        assert report["counts"]["run_records"] == "UNKNOWN"
        failure_ids = {row["question_id"] for row in report["examples"]["failures"]}
        assert failure_ids == {"q-000", "q-002_abs"}
        # No trace exists for q-000, so its wrong answer cannot be attributed
        # past the formation stage; the abstention question had no gold.
        counts = report["error_attribution"]["counts"]
        assert counts.get("FORMATION") == 1
        assert counts.get("READING") == 1

    def test_analyze_without_judge_reports_no_accuracy(self, tmp_path):
        dataset_path = self._evaluate_dataset(tmp_path)
        submission = tmp_path / "submission.jsonl"
        submission.write_text(
            json.dumps({"question_id": "q-000", "hypothesis": "Biscuit"}) + "\n",
            encoding="utf-8")
        report = analyze(dataset_path=dataset_path, submission_path=submission)
        assert report["accuracy"]["overall_accuracy"] is None
        assert report["accuracy"]["full_set_accuracy"] is None
        assert report["accuracy"]["by_question_type"]["single-session-user"]["accuracy"] is None
        assert report["accuracy"]["official_score"] == "NOT_EVALUATED"
        assert report["counts"]["judged"] == 0
        assert report["not_measured"]["official_overall_accuracy"] == "NOT_EVALUATED"

    def test_analyze_reads_run_traces_for_tokens_and_latency(self, tmp_path):
        dataset_path = self._evaluate_dataset(tmp_path)
        dataset = load_oracle_dataset(dataset_path, expected_cases=None)
        output = tmp_path / "run"
        OracleRunner(dataset=dataset, output_dir=output,
                     reader=DeterministicReader()).run()
        report = analyze(dataset_path=dataset_path,
                         submission_path=output / "submission.jsonl",
                         traces_dir=output / "traces",
                         summary_path=output / "summary.json",
                         judge=NormalizedExactMatchJudge())
        counts = report["counts"]
        assert counts["run_records"] == 3
        assert counts["successful"] == 3
        assert counts["judged"] == 3
        assert report["latency_ms"]["end_to_end"]["samples"] == 3
        assert report["latency_ms"]["end_to_end"]["p50"] is not None
        assert report["tokens"]["reader_input_total"] is not None

    def test_attribute_formation_retrieval_reading(self):
        formed = {"formation": {"turn_memory_counts": {"s1:0": 2},
                                "turn_events": {"s1:0": "evt-1"}}}
        # Gold never formed.
        assert attribute(formed, ["s9:0"], ["s9"], False, False) == "FORMATION"
        # Formed, never retrieved.
        record = {**formed, "retrieval": {"returned_source_refs": [["evt-7"]]}}
        assert attribute(record, ["s1:0"], ["s1"], False, False) == "RETRIEVAL"
        # Formed and retrieved; correctness decides.
        record = {**formed, "retrieval": {"returned_source_refs": [["evt-1"]]}}
        assert attribute(record, ["s1:0"], ["s1"], False, False) == "READING"
        assert attribute(record, ["s1:0"], ["s1"], True, False) == "CORRECT"
        # Abstention questions have no gold by construction.
        assert attribute({}, [], [], True, True) == "CORRECT"
        assert attribute({}, [], [], False, True) == "READING"

    def test_latency_block_percentiles(self):
        block = latency_block([1.0, 2.0, 3.0, 4.0, 100.0])
        assert block["samples"] == 5
        assert block["p50"] == 3.0
        assert block["p95"] == 100.0
        assert block["max"] == 100.0
        empty = latency_block([])
        assert empty["samples"] == 0 and empty["p50"] is None

    def test_estimate_cost(self):
        pricing = ModelPricing(input_usd_per_million=3.0, output_usd_per_million=6.0)
        assert estimate_cost(pricing, 1_000_000, 1_000_000) == pytest.approx(9.0)
        assert estimate_cost(None, 100, 100) is None
        assert estimate_cost(pricing, None, 100) is None

    def test_normalize_and_refusal_detection(self):
        assert normalize_answer("The answer is Biscuit!!") == "the answer is biscuit"
        assert normalize_answer("seven cats") == "7 cats"
        assert looks_like_refusal(CANONICAL_ABSTENTION_HYPOTHESIS)
        assert looks_like_refusal("There is no record of that.")
        assert not looks_like_refusal("Biscuit")

    def test_deterministic_judge_is_never_official(self):
        judge = NormalizedExactMatchJudge()
        assert judge.spec.official is False
        verdict = judge.judge(question="q", reference="Biscuit", hypothesis="Biscuit",
                               question_type="single-session-user")
        assert verdict.correct is True
        verdict = judge.judge(question="q", reference="Biscuit",
                              hypothesis="It was not Biscuit.",
                              question_type="single-session-user")
        assert verdict.correct is False
        verdict = judge.judge(question="q", reference="Biscuit", hypothesis="Waffles",
                               question_type="single-session-user")
        assert verdict.correct is False
        verdict = judge.judge(question="q", reference="unknown",
                               hypothesis=CANONICAL_ABSTENTION_HYPOTHESIS,
                               question_type="abstention")
        assert verdict.correct is True

    def test_correct_refusal_is_not_an_answered_only_hit(self, tmp_path):
        dataset_path = self._evaluate_dataset(tmp_path)
        submission = tmp_path / "submission.jsonl"
        submission.write_text(
            json.dumps({"question_id": "q-000", "hypothesis": "wrong"}) + "\n"
            + json.dumps({"question_id": "q-001", "hypothesis": "teal"}) + "\n"
            + json.dumps({"question_id": "q-002_abs",
                          "hypothesis": CANONICAL_ABSTENTION_HYPOTHESIS}) + "\n",
            encoding="utf-8",
        )
        accuracy = analyze(dataset_path=dataset_path, submission_path=submission,
                           judge=NormalizedExactMatchJudge())["accuracy"]
        assert accuracy["full_set_accuracy"] == pytest.approx(2 / 3)
        assert accuracy["answered_only_accuracy"] == pytest.approx(1 / 2)

    def test_failed_judge_keeps_full_set_denominator(self, tmp_path):
        dataset_path = self._evaluate_dataset(
            tmp_path, rows=[_oracle_row("q-000"), _oracle_row("q-001")]
        )
        submission = tmp_path / "submission.jsonl"
        submission.write_text(
            "\n".join(json.dumps({"question_id": qid, "hypothesis": "Biscuit"})
                      for qid in ("q-000", "q-001")) + "\n", encoding="utf-8",
        )

        class _FailingJudge:
            spec = JudgeSpec(provider="test", model="partial", official=True)
            calls = 0

            def judge(self, **_kwargs):
                self.calls += 1
                return (JudgeVerdict(True) if self.calls == 1
                        else JudgeVerdict(None, error="simulated judge failure"))

        report = analyze(dataset_path=dataset_path, submission_path=submission,
                         judge=_FailingJudge())
        block = report["accuracy"]["by_question_type"]["single-session-user"]
        assert block["cases"] == 2 and block["evaluated"] == 1
        assert block["accuracy_full_set"] == pytest.approx(0.5)
        assert block["accuracy_judged_only"] == pytest.approx(1.0)
        assert report["accuracy"]["score_type"] == "SELF_REPORTED_NON_OFFICIAL"
        assert report["accuracy"]["official_score"] == "NOT_EVALUATED"

    def test_local_cli_cannot_claim_official_judge(self):
        args = evaluate_parser().parse_args([
            "--dataset", "dataset.json", "--submission", "submission.jsonl",
            "--output", "report.json", "--judge", "exact", "--judge-official",
        ])
        with pytest.raises(SystemExit, match="unsupported"):
            build_judge(args)
