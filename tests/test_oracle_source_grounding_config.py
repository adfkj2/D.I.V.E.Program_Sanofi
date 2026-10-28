"""Opt-in source-grounded profiles keep historical Oracle identity unchanged."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_oracle_pipeline import _case, _dataset, _oracle_row
from test_reader_challenge_runner import challenge, dataset, execute_args
from test_staged_deliberation import Client

from dive_memory.application.oracle_run import main, r40_pipeline
from dive_memory.application.readers import create_reader
from dive_memory.dialogue_retention import PREDICATE, VERSION
from dive_memory.gate import GateAction, GateDecision
from dive_memory.oracle.contracts import OracleResumeConflictError
from dive_memory.oracle.pipeline import PipelineConfig, run_case
from dive_memory.oracle.reader import DeterministicReader
from dive_memory.oracle.runner import OracleRunner
from dive_memory.reading.reader import ChatCompletion


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            PipelineConfig(),
            "bfbfd871cfc6e72a1378655a04e023d6b6e76a377122db1b875ec25af48ff432",
        ),
        (
            r40_pipeline(),
            "4675336b522cea8589f0567358eab28c7ee7c8c8520d1d05ba507422efbaf273",
        ),
    ],
)
def test_disabled_dialogue_retention_preserves_pre_r68_serialization(config, expected):
    # Values were independently recorded before introducing the opt-in fields.
    assert _hash(config.to_dict()) == expected
    assert not any(key.startswith("dialogue_") for key in config.to_dict())


def test_enabled_policy_records_version_and_complete_bounds():
    config = PipelineConfig(
        dialogue_retention=True, dialogue_max_chars=113, dialogue_max_turn_chars=997
    )
    serialized = config.to_dict()
    assert serialized["dialogue_retention"] is True
    assert serialized["dialogue_retention_version"] == VERSION
    assert serialized["dialogue_max_chars"] == 113
    assert serialized["dialogue_max_turn_chars"] == 997


@pytest.mark.parametrize("enabled", [0, 1, "true", None])
def test_retention_selection_is_an_explicit_boolean(enabled):
    with pytest.raises(ValueError, match="boolean"):
        PipelineConfig(dialogue_retention=enabled)


@pytest.mark.parametrize(
    ("max_chars", "max_turn_chars"),
    [(0, 100), (-1, 100), (True, 100), (2.5, 100), (101, 100), (1, False), (1, 0)],
)
def test_invalid_retention_bounds_are_rejected_before_execution(
    max_chars, max_turn_chars
):
    with pytest.raises(ValueError, match="bounds"):
        PipelineConfig(
            dialogue_max_chars=max_chars, dialogue_max_turn_chars=max_turn_chars
        )


@pytest.mark.parametrize("strategy", ["deliberative-staged", "deliberative-grounded"])
@pytest.mark.parametrize("enabled", [False, True])
def test_cold_cli_selects_reader_and_retention_independently(
    strategy, enabled, tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("DIVE_READER_API_KEY", raising=False)
    output = tmp_path / "not-created"
    args = [
        "--dataset",
        "not-downloaded",
        "--output",
        str(output),
        "--reader-strategy",
        strategy,
    ]
    if enabled:
        args.extend(
            [
                "--dialogue-retention",
                "--dialogue-max-chars",
                "113",
                "--dialogue-max-turn-chars",
                "997",
            ]
        )
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "PLAN_ONLY" and not output.exists()
    assert plan["reader_strategy"] == strategy
    assert ("dialogue_retention" in plan["pipeline"]) is enabled
    if enabled:
        assert plan["pipeline"]["dialogue_max_chars"] == 113
        assert plan["pipeline"]["dialogue_max_turn_chars"] == 997


@pytest.mark.parametrize(
    "args",
    [
        ["--dialogue-max-chars", "0"],
        ["--dialogue-max-chars", "101", "--dialogue-max-turn-chars", "100"],
    ],
)
def test_invalid_cli_bounds_fail_in_plan_before_any_output(args, tmp_path, capsys):
    output = tmp_path / "not-created"
    with pytest.raises(SystemExit):
        main(["--dataset", "missing", "--output", str(output), *args])
    assert "bounds" in capsys.readouterr().err
    assert not output.exists()


def _skip(text, **kwargs):
    return GateDecision(False, 0, 0, 0, "ephemeral", "fixture", GateAction.SKIP)


class InspectReader(DeterministicReader):
    def generate(self, request):
        self.request = request
        return super().generate(request)


@pytest.mark.parametrize("enabled", [False, True])
def test_oracle_run_retains_only_admitted_bounded_user_observation(enabled):
    text = "I realized the brass object was worth triple what I paid."
    reader = InspectReader()
    case = _case(
        "synthetic-source-retention",
        question="What was the brass object worth?",
        sessions=[
            (
                "session",
                "2024-05-01T10:00:00Z",
                [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": "Assistant-only private detail."},
                ],
            )
        ],
    )
    result = run_case(
        case,
        reader=reader,
        gate=_skip,
        config=PipelineConfig(
            dialogue_retention=enabled,
            dialogue_max_chars=40,
            dialogue_max_turn_chars=200,
        ),
    )
    assert result.ok
    assert result.record["formation"]["memories_formed"] == int(enabled)
    items = reader.request.items
    if enabled:
        assert len(items) == 1 and items[0].memory.content == text[:40]
        structured = items[0].memory.structured_content
        assert structured["predicate"] == PREDICATE
        assert structured["truncated"] is True
        assert structured["source_span"] == [0, 40]
        assert "value" not in structured
    else:
        assert not items


@pytest.mark.parametrize(
    "text",
    [
        "Do not remember this. The brass object is worth triple what I paid.",
        "My password is private-credential-fixture.",
        "A very long turn. " * 30,
    ],
)
def test_oracle_retention_does_not_bypass_optout_sensitive_or_full_turn_cap(text):
    reader = InspectReader()
    result = run_case(
        _case(
            sessions=[
                (
                    "s",
                    "2024-05-01T10:00:00Z",
                    [
                        {"role": "user", "content": text},
                    ],
                )
            ]
        ),
        reader=reader,
        gate=_skip,
        config=PipelineConfig(
            dialogue_retention=True,
            dialogue_max_chars=40,
            dialogue_max_turn_chars=200,
        ),
    )
    assert result.ok and result.record["formation"]["memories_formed"] == 0
    assert not reader.request.items


def test_oracle_retention_filters_future_turn_before_projection():
    reader = InspectReader()
    result = run_case(
        _case(
            question="What was the past text?",
            question_date="2024-05-02T00:00:00Z",
            sessions=[
                (
                    "past",
                    "2024-05-01T10:00:00Z",
                    [{"role": "user", "content": "Past text."}],
                ),
                (
                    "future",
                    "2024-05-03T10:00:00Z",
                    [{"role": "user", "content": "Future secret."}],
                ),
            ],
        ),
        reader=reader,
        gate=_skip,
        config=PipelineConfig(dialogue_retention=True),
    )
    assert result.ok and result.record["formation"]["turns_ingested"] == 1
    assert result.record["chronology"]["turns_after_question_date"] == ["future:0"]
    assert [item.memory.content for item in reader.request.items] == ["Past text."]


def test_dialogue_retention_composes_with_existing_r40_retention_layers():
    text = "I realized the brass object was worth triple what I paid."
    config = replace(
        r40_pipeline(), semantic_retrieval_only=False, dialogue_retention=True
    )
    reader = InspectReader()
    result = run_case(
        _case(
            question="What was the brass object worth?",
            sessions=[
                ("s", "2024-05-01T10:00:00Z", [{"role": "user", "content": text}]),
            ],
        ),
        reader=reader,
        gate=_skip,
        config=config,
    )
    assert result.ok
    retained = [
        item
        for item in reader.request.items
        if item.memory.structured_content.get("predicate") == PREDICATE
    ]
    assert len(retained) == 1 and retained[0].memory.content == text


@pytest.mark.parametrize(
    "field",
    [
        "dialogue_retention",
        "dialogue_max_chars",
        "dialogue_max_turn_chars",
    ],
)
def test_resume_refuses_changed_retention_selection_or_bound(tmp_path, field):
    data = _dataset(tmp_path, [_oracle_row("synthetic-retention-resume")])
    config = PipelineConfig(dialogue_retention=True)
    runner = OracleRunner(
        data, tmp_path / "run", DeterministicReader(), pipeline_config=config
    )
    old_key = runner.resume_key()
    changes = {
        "dialogue_retention": False,
        "dialogue_max_chars": 1999,
        "dialogue_max_turn_chars": 15999,
    }
    runner.pipeline_config = replace(config, **{field: changes[field]})
    assert runner.resume_key() != old_key
    with pytest.raises(OracleResumeConflictError, match="differs"):
        runner._assert_resume_compatible({"resume_key": old_key}, resume=True)


def test_resume_identity_binds_retention_version(tmp_path, monkeypatch):
    from dive_memory.oracle import pipeline

    data = _dataset(tmp_path, [_oracle_row("synthetic-retention-version")])
    runner = OracleRunner(
        data,
        tmp_path / "run",
        DeterministicReader(),
        pipeline_config=PipelineConfig(dialogue_retention=True),
    )
    key = runner.resume_key()
    monkeypatch.setattr(
        pipeline, "DIALOGUE_RETENTION_VERSION", "future-synthetic-version"
    )
    assert runner.resume_key() != key
    with pytest.raises(OracleResumeConflictError, match="differs"):
        runner._assert_resume_compatible({"resume_key": key}, resume=True)


def test_grounded_reader_identity_binds_source_and_snapshot_profile(tmp_path):
    data = _dataset(tmp_path, [_oracle_row("synthetic-reader-profile")])
    ordinary, thinking = Client([]), Client([])
    grounded = create_reader(
        ordinary,
        thinking,
        model="fixture",
        strategy="deliberative-grounded",
        reasoning_effort="high",
    )
    staged = create_reader(
        ordinary,
        thinking,
        model="fixture",
        strategy="deliberative-staged",
        reasoning_effort="high",
    )
    runner = OracleRunner(
        data,
        tmp_path / "run",
        grounded,
        pipeline_config=PipelineConfig(reader_policy_name="indexed_evidence"),
    )
    execution = runner.configuration()["reader_execution"]
    assert execution["evidence_configuration"] == {
        "workspace_identity": "admitted-v1",
        "annotate_sources": True,
    }
    assert execution["phases"]["synthesize"]["reasoning_effort"] == "high"
    assert execution["phases"]["synthesize"]["spec"]["max_tokens"] == 16384
    key = runner.resume_key()
    runner.reader = staged
    assert runner.resume_key() != key
    assert not ordinary.calls and not thinking.calls


def test_grounded_oracle_uses_admitted_retention_and_live_source_metadata():
    plan = {
        "source_indices": [0],
        "queries": [],
        "reads": [],
        "question_contract": {
            "target": "brass object's relative value",
            "answer_unit": "ratio",
            "include": [],
            "exclude": [],
            "question_premises": [],
            "required_details": [],
        },
    }
    decision = {
        "status": "ANSWER",
        "answer": "Triple what I paid.",
        "source_indices": [0],
        "missing": [],
    }
    draft = {
        **decision,
        "analysis": {
            "scope": "brass object's value",
            "events": [
                {
                    "claim": "Object was worth triple the paid amount.",
                    "source_indices": [0],
                    "status": "reported",
                    "time": "unknown",
                    "group": "brass-object",
                }
            ],
            "answer_details": [],
            "uncertainties": [],
        },
    }
    review = {**decision, "review": {"action": "accept", "defects": []}}
    ordinary, thinking = Client([plan]), Client([draft, review])
    reader = create_reader(
        ordinary, thinking, model="fixture", strategy="deliberative-grounded"
    )
    result = run_case(
        _case(
            question="What was the brass object worth?",
            sessions=[
                (
                    "source-session",
                    "2024-05-01T10:00:00Z",
                    [
                        {
                            "role": "user",
                            "content": "I realized the brass object was worth triple what I paid.",
                        }
                    ],
                )
            ],
        ),
        reader=reader,
        gate=_skip,
        config=PipelineConfig(
            reader_policy_name="indexed_evidence",
            dialogue_retention=True,
        ),
    )
    assert result.ok and result.hypothesis == "Triple what I paid."
    assert result.record["formation"]["memories_formed"] == 1
    assert len(ordinary.calls) == 1 and len(thinking.calls) == 2
    for call in [*ordinary.calls, *thinking.calls]:
        prompt = json.dumps(call["messages"])
        assert "@statement_at=2024-05-01T10:00:00+00:00" in prompt
        assert "role=user" in prompt
        assert "Biscuit" not in prompt  # The fixture's evaluator answer is withheld.


def test_grounded_challenge_cli_records_profiles_and_excludes_evaluator_metadata(
    tmp_path, monkeypatch
):
    inputs, output = dataset(tmp_path), tmp_path / "grounded-run"
    responses = []

    class FakeTransport:
        endpoint = "fixture://no-network"

        def __init__(self, endpoint, key, **kwargs):
            self.extra_body = kwargs.get("extra_body", {})

        def complete(self, **kwargs):
            rules = kwargs["messages"][0]["content"]
            if "Gather evidence before answering" in rules:
                value = {
                    "source_indices": [0],
                    "queries": [],
                    "reads": [],
                    "question_contract": {
                        "target": "approved budget",
                        "answer_unit": "amount",
                        "include": [],
                        "exclude": [],
                        "question_premises": [],
                        "required_details": [],
                    },
                }
            else:
                value = {
                    "status": "ANSWER",
                    "answer": "73",
                    "source_indices": [0],
                    "missing": [],
                }
                if "additional audit field is analysis" in rules:
                    value["analysis"] = {
                        "scope": "approved budget",
                        "events": [
                            {
                                "claim": "Budget is 73",
                                "source_indices": [0],
                                "status": "reported",
                                "time": "2026-04-01",
                                "group": "budget",
                            }
                        ],
                        "answer_details": [],
                        "uncertainties": [],
                    }
                else:
                    value["review"] = {"action": "accept", "defects": []}
            response = json.dumps(value)
            responses.append(response)
            return ChatCompletion(response, "fixture", input_tokens=10, output_tokens=5)

    monkeypatch.setenv("DIVE_READER_API_KEY", "fixture-secret-only-in-memory")
    monkeypatch.setattr(challenge, "OpenAICompatibleChatClient", FakeTransport)
    args = execute_args(inputs, output)
    args[args.index("--strategy") + 1] = "deliberative-grounded"
    assert challenge.main(args) == 0
    plan = json.loads((output / "plan.json").read_text())
    assert (
        plan["reader_execution"]["evidence_configuration"]["workspace_identity"]
        == "admitted-v1"
    )
    records = [
        json.loads(path.read_text())
        for path in sorted(output.glob("request-*.json"))
        if path.name != "request-ledger.json"
    ]
    assert len(records) == 3 and [record["response"] for record in records] == responses
    assert [record["mode"]["thinking"]["type"] for record in records] == [
        "disabled",
        "enabled",
        "enabled",
    ]
    assert [record["request"]["max_tokens"] for record in records] == [
        8192,
        16384,
        16384,
    ]
    serialized = json.dumps(records)
    for sentinel in (
        "private-expected-metadata",
        "case-metadata-0",
        "category-metadata-never-input",
        "fixture-secret-only-in-memory",
    ):
        assert sentinel not in serialized
