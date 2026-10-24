"""Challenge execution isolation, temporal scope and hard budget boundaries."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from dive_memory.application.oracle_run import RunStopped
from dive_memory.application.readers import create_reader
from dive_memory.reading.evidence import build_evidence_views, render_evidence_block
from dive_memory.reading.providers import ScriptedChatClient
from dive_memory.reading.reader import ChatCompletion
from dive_memory.token_budget import ApproximateTokenCounter

spec = importlib.util.spec_from_file_location(
    "reader_challenge_runner",
    Path(__file__).parents[1] / "tools/run_reader_challenge.py",
)
assert spec and spec.loader
challenge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(challenge)


def case(number=0):
    return {
        "id": f"case-metadata-{number}",
        "category": "category-metadata-never-input",
        "language": "en",
        "date": "2026-04-10T12:00:00Z",
        "question": "What is the approved budget?",
        "sources": [
            {
                "id": "statement-a",
                "role": "user",
                "date": "2026-04-01T12:00:00Z",
                "content": "The approved budget is 73.",
            }
        ],
        "expected": {"never-prompt": "private-expected-metadata"},
    }


def dataset(tmp_path, cases=None):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "reader-challenge-v1",
                "dataset_kind": "handwritten_development_fixtures",
                "independent_holdout": False,
                "cases": cases if cases is not None else [case()],
            }
        ),
        encoding="utf-8",
    )
    return path


ANSWER = {
    "status": "ANSWER",
    "answer": "73",
    "citations": [{"index": 0, "quote": "approved budget is 73"}],
    "missing": [],
}


def test_case_id_category_and_expected_never_reach_any_model_phase():
    client = ScriptedChatClient(
        [
            json.dumps({"notes": [], "queries": [], "reads": []}),
            json.dumps(ANSWER),
            json.dumps(ANSWER),
        ],
        input_tokens_per_call=10,
        output_tokens_per_call=5,
    )
    reader = create_reader(client, strategy="deliberative", model="fixture")
    original = case()
    snapshot = deepcopy(original)
    result = challenge.run_case(original, reader)
    assert result["reader"]["status"] == "ANSWER"
    assert len(client.prompts) == 3
    for prompt in client.prompts:
        for marker in (
            original["id"],
            original["category"],
            "private-expected-metadata",
            "never-prompt",
        ):
            assert marker not in prompt
    assert original == snapshot
    assert result["semantic_correctness"] is None
    assert result["evaluation"].startswith("NOT_EVALUATED")


def test_future_filter_compares_instants_across_timezone_offsets():
    original = case()
    original["date"] = "2026-04-10T01:00:00+02:00"
    source = original["sources"][0]
    original["sources"] = [
        {**source, "id": "future-lexically-earlier", "date": "2026-04-10T00:30:00Z"},
        {
            **source,
            "id": "past-with-later-local-time",
            "date": "2026-04-10T00:30:00+03:00",
        },
    ]
    request, source = challenge.build_request(original)
    assert {ref for item in request.items for ref in item.source_refs} == {
        "past-with-later-local-time"
    }
    assert {ref for item in source.search("budget") for ref in item.source_refs} == {
        "past-with-later-local-time"
    }


def test_initial_context_is_bounded_and_read_requires_discovery():
    original = case()
    prototype = original["sources"][0]
    original["sources"] = [
        {
            **prototype,
            "id": f"source-ref-{number}",
            "content": f"Project component {number}. " + "filler text " * 160,
        }
        for number in range(12)
    ]
    request, source = challenge.build_request(original, token_budget=300)
    text = render_evidence_block(
        build_evidence_views(
            request.items,
            question=request.question,
            question_date=request.question_date,
        )
    )
    assert ApproximateTokenCounter().count(text) <= 300
    assert request.items
    hidden = set(source.items) - source.discovered
    assert hidden
    with pytest.raises(ValueError, match="scope"):
        source.read(next(iter(hidden)))
    discovered = source.search("component")
    assert discovered
    assert source.read(discovered[-1].memory.id) is discovered[-1]
    with pytest.raises(ValueError, match="scope"):
        source.read("fabricated-memory")


def test_default_plan_has_zero_requests_and_creates_no_output(
    tmp_path, monkeypatch, capsys
):
    path = dataset(tmp_path)
    output = tmp_path / "uncreated"

    def forbidden(*args, **kwargs):
        raise AssertionError("A plan must never create a transport")

    monkeypatch.setattr(challenge, "OpenAICompatibleChatClient", forbidden)
    monkeypatch.delenv("DIVE_READER_API_KEY", raising=False)
    assert challenge.main(["--dataset", str(path), "--output", str(output)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "PLAN_ONLY"
    assert plan["max_calls"] == plan["max_reserved_tokens"] == 0
    assert plan["semantic_evaluation"] == "NOT_EVALUATED"
    assert plan["official_benchmark"] is False
    assert not output.exists()


@pytest.mark.parametrize(
    "caps",
    [
        [],
        ["--max-calls", "1", "--max-reserved-tokens", "100000"],
        [
            "--max-calls",
            "1",
            "--max-reserved-tokens",
            "100000",
            "--max-cost-cny",
            "NaN",
        ],
        ["--max-calls", "1", "--max-reserved-tokens", "100000", "--max-cost-cny", "-1"],
    ],
)
def test_execute_requires_all_positive_finite_caps_before_transport(
    tmp_path, monkeypatch, caps
):
    path = dataset(tmp_path)
    output = tmp_path / "uncreated"
    monkeypatch.setenv("DIVE_READER_API_KEY", "synthetic-not-a-real-key")

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid caps cannot create a transport")

    monkeypatch.setattr(challenge, "OpenAICompatibleChatClient", forbidden)
    with pytest.raises(SystemExit) as error:
        challenge.main(
            ["--dataset", str(path), "--output", str(output), "--execute", *caps]
        )
    assert error.value.code == 2
    assert not output.exists()


def execute_args(path, output, *, calls=10, tokens=100000, cost="10"):
    return [
        "--dataset",
        str(path),
        "--output",
        str(output),
        "--execute",
        "--strategy",
        "indexed",
        "--max-calls",
        str(calls),
        "--max-reserved-tokens",
        str(tokens),
        "--max-cost-cny",
        cost,
    ]


def fake_transport(monkeypatch, *, fail=False, unknown_usage=False):
    calls = []

    class FakeTransport:
        endpoint = "fixture://no-network"

        def __init__(self, *args, **kwargs):
            self.extra_body = kwargs.get("extra_body", {})

        def complete(self, **kwargs):
            calls.append(kwargs)
            if fail:
                raise OSError("synthetic transport failure")
            return ChatCompletion(
                json.dumps(ANSWER),
                "fixture",
                input_tokens=None if unknown_usage else 10,
                output_tokens=None if unknown_usage else 5,
            )

    monkeypatch.setenv("DIVE_READER_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setattr(challenge, "OpenAICompatibleChatClient", FakeTransport)
    return calls


@pytest.mark.parametrize("cap", ["calls", "tokens", "currency"])
def test_budget_stop_returns_incomplete_and_does_not_attempt_another_case(
    tmp_path, monkeypatch, cap
):
    path = dataset(tmp_path, [case(0), case(1), case(2)])
    output = tmp_path / "run"
    calls = fake_transport(monkeypatch)
    args = execute_args(
        path,
        output,
        calls=1 if cap == "calls" else 10,
        tokens=1 if cap == "tokens" else 100000,
        cost="0.000001" if cap == "currency" else "10",
    )
    assert challenge.main(args) == 2
    assert len(calls) == (1 if cap == "calls" else 0)
    assert len(list(output.glob("request-[0-9][0-9][0-9][0-9].json"))) == len(calls)
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["completed"] < 3
    assert not (output / "case-002.json").exists()
    assert (output / "stopped.json").exists()


@pytest.mark.parametrize("failure", ["transport", "usage"])
def test_uncertain_ledger_stops_even_when_reader_swallows_budget_stop(
    tmp_path, monkeypatch, failure
):
    path = dataset(tmp_path, [case(0), case(1)])
    output = tmp_path / "run"
    calls = fake_transport(
        monkeypatch, fail=failure == "transport", unknown_usage=failure == "usage"
    )
    attempted = []

    def swallow(case_value, reader):
        attempted.append(case_value["id"])
        try:
            reader.client.complete(messages=[], max_tokens=1)
        except RunStopped:
            pass
        return {"case_id": case_value["id"], "reader": {"status": "ANSWER"}}

    monkeypatch.setattr(challenge, "run_case", swallow)
    assert challenge.main(execute_args(path, output)) == 2
    assert len(calls) == len(attempted) == 1
    assert not (output / "case-001.json").exists()
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["usage"]["input_tokens"] is None
    assert summary["usage"]["output_tokens"] is None


def test_old_output_is_never_overwritten_or_reused(tmp_path, monkeypatch):
    path = dataset(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "summary.json"
    sentinel.write_text("original evidence", encoding="utf-8")
    calls = fake_transport(monkeypatch)
    with pytest.raises((SystemExit, FileExistsError)):
        challenge.main(execute_args(path, output))
    assert sentinel.read_text(encoding="utf-8") == "original evidence"
    assert calls == []


@pytest.mark.parametrize("strategy", ["indexed", "r40", "verified", "deliberative"])
def test_all_strategies_record_exact_raw_replies_without_credentials(
    tmp_path, monkeypatch, strategy
):
    path = dataset(tmp_path)
    output = tmp_path / "recorded"
    secret = "fixture-secret-token"
    responses = []

    class FakeTransport:
        endpoint = "fixture://no-network"

        def __init__(self, endpoint, api_key, **kwargs):
            # A credential exists on the transport but must never be serialized.
            self.api_key = api_key
            self.extra_body = kwargs.get("extra_body", {})

        def complete(self, **kwargs):
            planning = "First gather evidence" in kwargs["messages"][0]["content"]
            value = {"notes": [], "queries": [], "reads": []} if planning else ANSWER
            raw = " \n" + json.dumps(value) + "\n "
            responses.append(raw)
            return ChatCompletion(raw, "fixture", input_tokens=10, output_tokens=5)

    monkeypatch.setenv("DIVE_READER_API_KEY", secret)
    monkeypatch.setattr(challenge, "OpenAICompatibleChatClient", FakeTransport)
    args = execute_args(path, output)
    args[args.index("--strategy") + 1] = strategy
    assert challenge.main(args) == 0
    records = [
        json.loads(record.read_text(encoding="utf-8"))
        for record in sorted(output.glob("request-[0-9][0-9][0-9][0-9].json"))
    ]
    assert records and [record["response"] for record in records] == responses
    assert all(record["state"] == "completed" for record in records)
    assert all(record["request"]["messages"] for record in records)
    for artifact in output.glob("*.json"):
        assert secret not in artifact.read_text(encoding="utf-8")
    for record in records:
        serialized = json.dumps(record)
        assert "private-expected-metadata" not in serialized
        assert "case-metadata-0" not in serialized
        assert "category-metadata-never-input" not in serialized
