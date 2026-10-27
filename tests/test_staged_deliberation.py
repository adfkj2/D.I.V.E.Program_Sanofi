import json
from dataclasses import replace

import pytest
from test_deliberative_reader import Source, item, request
from test_oracle_pipeline import _dataset, _oracle_row

from dive_memory.application.readers import create_reader
from dive_memory.oracle.contracts import OracleResumeConflictError
from dive_memory.oracle.pipeline import PipelineConfig
from dive_memory.oracle.runner import OracleRunner
from dive_memory.reading.reader import ChatCompletion

PLAN = {"source_indices": [0], "queries": [], "reads": []}
ANSWER = {"status": "ANSWER", "answer": "73", "source_indices": [0], "missing": []}


class Client:
    provider = "fixture"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, ChatCompletion):
            return value
        return ChatCompletion(
            json.dumps(value), "fixture", input_tokens=10, output_tokens=5
        )


def setup(normal_responses, thinking_responses):
    normal, thinking = Client(normal_responses), Client(thinking_responses)
    reader = create_reader(
        normal, thinking, model="fixture", strategy="deliberative-staged"
    )
    return reader.bind_evidence(Source()), normal, thinking


def test_plan_uses_normal_and_two_decision_phases_use_larger_reasoning_budget():
    reader, normal, thinking = setup([PLAN], [ANSWER, ANSWER])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.hypothesis == "73" and result.calls == 3
    assert [call["max_tokens"] for call in normal.calls] == [8192]
    assert [call["max_tokens"] for call in thinking.calls] == [16384, 16384]
    assert result.usage.output_tokens == 15
    trace = json.loads(result.detail)
    assert trace["execution_policy"]["version"] == "staged-completion-v1"
    assert len(trace["execution_policy_sha256"]) == 64
    assert [turn["call_profile"] for turn in trace["turns"]] == [
        "normal",
        "reasoning",
        "reasoning",
    ]


def test_explicit_reasoning_transport_is_required():
    with pytest.raises(ValueError, match="explicit"):
        create_reader(Client([]), model="fixture", strategy="deliberative-staged")


@pytest.mark.parametrize("phase", ["plan", "synthesize", "review"])
def test_empty_completion_recovers_once_with_normal_model_and_finishes_all_phases(
    phase,
):
    empty = ChatCompletion("", "fixture", input_tokens=10, output_tokens=8192)
    normal_values = [empty, PLAN] if phase == "plan" else [PLAN, ANSWER]
    thinking_values = (
        [ANSWER, ANSWER]
        if phase == "plan"
        else [empty, ANSWER]
        if phase == "synthesize"
        else [ANSWER, empty]
    )
    reader, normal, thinking = setup(normal_values, thinking_values)
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.hypothesis == "73" and result.calls == 4
    assert len(normal.calls) == 2 and len(thinking.calls) == 2
    audit = json.loads(result.detail)
    assert audit["turns"][-1]["phase"] == "review"
    recovery = next(turn for turn in audit["turns"] if turn.get("recovery"))
    assert recovery["phase"] == phase and recovery["call_profile"] == "normal"
    assert result.usage.input_tokens == 40 and result.usage.output_tokens == 8207


def test_unknown_usage_stops_before_repair_even_for_valid_json():
    response = ChatCompletion(
        json.dumps(PLAN), "fixture", input_tokens=None, output_tokens=5
    )
    reader, normal, thinking = setup([response, PLAN], [ANSWER, ANSWER])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.reason_code == "READER_USAGE_UNKNOWN" and result.is_failure
    assert len(normal.calls) == 1 and not thinking.calls
    assert result.usage.source == "unknown" and result.hypothesis is None


def test_recovery_does_not_add_a_fifth_call_or_reuse_a_draft():
    reader, normal, thinking = setup(["invalid", PLAN], [ANSWER, "invalid again"])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.is_failure and result.hypothesis is None and result.calls == 4
    assert len(normal.calls) == len(thinking.calls) == 2


def test_transport_failure_is_never_retried():
    reader, normal, thinking = setup([TimeoutError("synthetic")], [])
    result = reader.generate(request())
    assert result.status.value == "TIMEOUT" and result.calls == 1
    assert result.usage.source == "unknown" and not thinking.calls


def test_legacy_reference_strategy_retains_one_client_and_original_budget():
    normal, thinking = Client([]), Client([PLAN, ANSWER, ANSWER])
    reader = create_reader(
        normal, thinking, model="fixture", strategy="deliberative-refs"
    )
    result = reader.bind_evidence(Source()).generate(
        request([item("The budget is 73.")])
    )
    assert result.hypothesis == "73" and not normal.calls
    assert [call["max_tokens"] for call in thinking.calls] == [8192] * 3
    assert "execution_policy" not in json.loads(result.detail)


@pytest.mark.parametrize("phase", ["plan", "synthesize", "review"])
@pytest.mark.parametrize("text", ['{"secret_partial_marker":', json.dumps(PLAN)])
def test_length_always_uses_shared_recovery_without_forwarding_partial_content(
    phase, text
):
    truncated = ChatCompletion(
        text,
        "fixture",
        input_tokens=10,
        output_tokens=16384,
        finish_reason="length",
        reasoning_tokens=16000,
    )
    normal_values = [truncated, PLAN] if phase == "plan" else [PLAN, ANSWER]
    thinking_values = (
        [ANSWER, ANSWER]
        if phase == "plan"
        else [truncated, ANSWER]
        if phase == "synthesize"
        else [ANSWER, truncated]
    )
    reader, normal, thinking = setup(normal_values, thinking_values)
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.hypothesis == "73" and result.calls == 4
    assert result.usage.output_tokens == 16399
    # Recovery starts from the same sources and phase input, never partial text.
    recovery_payload = json.loads(normal.calls[-1]["messages"][1]["content"])
    assert "secret_partial_marker" not in json.dumps(recovery_payload)
    assert "truncated" in recovery_payload["validation_error"]
    audit = json.loads(result.detail)
    failed = next(turn for turn in audit["turns"] if turn.get("completion_problem"))
    assert failed["finish_reason"] == "length" and failed["reasoning_tokens"] == 16000
    assert audit["turns"][-1]["phase"] == "review"


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("content_filter", "READER_CONTENT_FILTERED"),
        ("tool_calls", "READER_UNEXPECTED_FINISH"),
        ("insufficient_system_resource", "READER_UNEXPECTED_FINISH"),
        ("vendor-other", "READER_UNEXPECTED_FINISH"),
    ],
)
def test_nonrecoverable_termination_stops_even_with_parseable_json(reason, expected):
    response = ChatCompletion(
        json.dumps(PLAN),
        "fixture",
        input_tokens=10,
        output_tokens=5,
        finish_reason=reason,
    )
    reader, normal, thinking = setup([response, PLAN], [ANSWER, ANSWER])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.reason_code == expected and result.is_failure and result.calls == 1
    assert result.usage.source == "provider" and not thinking.calls


def test_second_truncation_stops_without_a_draft_answer():
    truncated = ChatCompletion(
        "", "fixture", input_tokens=10, output_tokens=16384, finish_reason="length"
    )
    reader, normal, thinking = setup([PLAN, ANSWER], [truncated, truncated])
    result = reader.generate(request([item("The budget is 73.")]))
    assert result.reason_code == "OUTPUT_TRUNCATED" and result.hypothesis is None
    assert result.calls == 4 and len(normal.calls) == 2 and len(thinking.calls) == 2


def test_execution_budget_is_preserved_by_evidence_binding_and_in_its_fingerprint():
    reader, normal, thinking = setup([PLAN], [ANSWER, ANSWER])
    assert reader.execution is not None
    custom = replace(
        reader.execution,
        plan=replace(
            reader.execution.plan,
            spec=replace(reader.execution.plan.spec, max_tokens=4096),
        ),
    )
    reader.execution = custom
    bound = reader.bind_evidence(Source())
    assert bound.execution is custom
    result = bound.generate(request([item("The budget is 73.")]))
    audit = json.loads(result.detail)
    assert audit["execution_policy"]["plan"]["spec"]["max_tokens"] == 4096
    assert normal.calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize(
    "changed", ["plan_budget", "reasoning_effort", "call_cap", "passage_chars"]
)
def test_resume_rejects_changed_phase_configuration_without_sending(tmp_path, changed):
    dataset = _dataset(tmp_path, [_oracle_row("synthetic-staged")])
    reader, normal, thinking = setup([], [])
    runner = OracleRunner(
        dataset,
        tmp_path / "run",
        reader,
        pipeline_config=PipelineConfig(reader_policy_name="indexed_evidence"),
    )
    original = runner.resume_key()
    original_spec = reader.spec.fingerprint()
    if changed == "plan_budget":
        reader.execution = replace(
            reader.execution,
            plan=replace(
                reader.execution.plan,
                spec=replace(reader.execution.plan.spec, max_tokens=4096),
            ),
        )
    elif changed == "reasoning_effort":
        reader.execution = replace(
            reader.execution,
            synthesize=replace(reader.execution.synthesize, reasoning_effort="max"),
        )
    elif changed == "call_cap":
        reader.max_calls = 3
    else:
        reader.passage_chars = 400
    assert reader.spec.fingerprint() == original_spec
    assert runner.resume_key() != original
    with pytest.raises(OracleResumeConflictError, match="differs"):
        runner._assert_resume_compatible({"resume_key": original}, resume=True)
    assert not normal.calls and not thinking.calls


def test_legacy_reader_does_not_add_execution_configuration(tmp_path):
    dataset = _dataset(tmp_path, [_oracle_row("synthetic-legacy")])
    reader = create_reader(Client([]), model="fixture", strategy="deliberative-refs")
    assert (
        "reader_execution"
        not in OracleRunner(
            dataset,
            tmp_path / "run",
            reader,
            pipeline_config=PipelineConfig(reader_policy_name="indexed_evidence"),
        ).configuration()
    )
