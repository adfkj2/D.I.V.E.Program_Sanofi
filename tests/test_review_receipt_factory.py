"""Receipt review is an explicit strategy with unchanged transport boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest

from dive_memory.application.oracle_run import main as oracle_main
from dive_memory.application.readers import create_reader


class UncalledClient:
    provider = "fixture"

    def complete(self, **kwargs):
        raise AssertionError("Factory and plan operations must not call a provider")


def test_receipt_strategy_selects_explicit_protocol_and_original_phase_limits():
    reader = create_reader(
        UncalledClient(),
        UncalledClient(),
        model="fixture",
        strategy="deliberative-grounded-receipt",
        reasoning_effort="high",
    )
    assert reader.spec.prompt_version == "argument-grounded-review-receipt-v1"
    assert reader.protocol.resolve_review is not None
    configuration = reader.execution_configuration()
    assert (
        configuration["evidence_configuration"]["workspace_identity"] == "admitted-v1"
    )
    assert configuration["evidence_configuration"]["annotate_sources"] is True
    phases = configuration["phases"]
    assert phases["plan"]["profile"] == phases["recovery"]["profile"] == "normal"
    assert phases["synthesize"]["profile"] == phases["review"]["profile"] == "reasoning"
    assert phases["plan"]["spec"]["max_tokens"] == 8192
    assert phases["review"]["spec"]["max_tokens"] == 16384
    assert phases["review"]["reasoning_effort"] == "high"


def test_old_grounded_protocol_remains_explicitly_unchanged():
    reader = create_reader(
        UncalledClient(),
        UncalledClient(),
        model="fixture",
        strategy="deliberative-grounded",
    )
    assert reader.spec.prompt_version == "argument-grounded-reader-v1"
    assert reader.protocol.resolve_review is None


def test_receipt_mode_requires_the_separate_reasoning_transport():
    with pytest.raises(ValueError, match="explicit"):
        create_reader(
            UncalledClient(), model="fixture", strategy="deliberative-grounded-receipt"
        )


def test_oracle_cold_plan_accepts_receipt_without_key_data_or_output(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("DIVE_READER_API_KEY", raising=False)
    target = tmp_path / "not-created"
    assert (
        oracle_main(
            [
                "--dataset",
                "not-downloaded",
                "--output",
                str(target),
                "--reader-strategy",
                "deliberative-grounded-receipt",
                "--dialogue-retention",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "PLAN_ONLY"
    assert plan["reader_strategy"] == "deliberative-grounded-receipt"
    assert plan["pipeline"]["dialogue_retention"] is True
    assert not target.exists()


def test_challenge_plan_accepts_receipt_without_credential_or_network(
    tmp_path, capsys, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    specification = importlib.util.spec_from_file_location(
        "receipt_challenge_plan", root / "tools/run_reader_challenge.py"
    )
    challenge = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(challenge)
    monkeypatch.delenv("DIVE_READER_API_KEY", raising=False)
    monkeypatch.setattr(
        challenge,
        "OpenAICompatibleChatClient",
        lambda *args, **kwargs: pytest.fail("No provider may be constructed by a plan"),
    )
    target = tmp_path / "not-created"
    assert (
        challenge.main(
            [
                "--dataset",
                str(root / "eval/datasets/reader_challenge_v1/cases.json"),
                "--output",
                str(target),
                "--strategy",
                "deliberative-grounded-receipt",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "PLAN_ONLY" and plan["cases"] == 32
    assert plan["strategy"] == "deliberative-grounded-receipt"
    assert not target.exists()
