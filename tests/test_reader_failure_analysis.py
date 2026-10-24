"""Independent diagnostics must preserve verdict and missing-data boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "reader_failure_analysis",
    Path(__file__).parents[1] / "tools/analyze_reader_failures.py",
)
assert spec and spec.loader
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def turn(*sources, action=None, error=None):
    result = {
        "context": "\n".join(
            f"[{i}] id=m{i}; source={source}; valid=unknown; role=user\n    evidence"
            for i, source in enumerate(sources)
        )
    }
    if action is not None:
        result["action"] = action
    if error is not None:
        result["validation_error"] = error
    return result


def trace(qid, turns, *, tokens=None):
    return {
        "question_id": qid,
        "reader": {"detail": json.dumps({"turns": turns})},
        "tokens": tokens or {},
    }


def evaluation(tmp_path, rows):
    return write(tmp_path / "evaluation.json", {"selected": len(rows), "results": rows})


def test_full_denominator_unknowns_and_missing_trace_stay_separate(tmp_path):
    path = evaluation(
        tmp_path,
        [
            {"question_id": "a", "correct": True, "status": "ANSWER"},
            {"question_id": "b", "correct": False, "status": "INVALID"},
            {"question_id": "c", "correct": None, "status": "ABSTAIN"},
        ],
    )
    directory = tmp_path / "traces"
    write(directory / "a.json", trace("a", [turn("s1")]))
    write(
        directory / "b.json",
        trace("b", [turn("s2", error="missing field")]),
    )
    result = analysis.analyze([directory], path)
    assert result["selected"] == 3
    assert result["confirmed_correct"] == 1
    assert result["confirmed_wrong"] == 1
    assert result["unknown_verdicts"] == 1
    assert result["confirmed_accuracy"] == 1 / 3
    assert result["missing_traces"] == 1
    assert result["unknown_reader_call_cases"] == 1
    assert result["invalid_final_types"] == {"missing field": 1}


def test_each_transition_tracks_sources_not_memory_ids_or_final_only(tmp_path):
    path = evaluation(
        tmp_path, [{"question_id": "a", "correct": False, "status": "ANSWER"}]
    )
    directory = tmp_path / "traces"
    write(
        directory / "a.json",
        trace("a", [turn("one", "two"), turn("two", "three"), turn("one", "two")]),
    )
    result = analysis.analyze([directory], path)["source_visibility"]
    assert result["cases_with_initial_source_lost"] == 0
    assert result["transitions_with_source_lost"] == 2
    assert result["rows"][0]["transitions"][0]["lost_source_refs"] == ["one"]
    assert result["rows"][0]["transitions"][1]["lost_source_refs"] == ["three"]


def test_calculation_failures_are_not_converted_to_success(tmp_path):
    path = evaluation(
        tmp_path, [{"question_id": "a", "correct": False, "status": "ANSWER"}]
    )
    directory = tmp_path / "traces"
    failed = turn("one", action={"action": "calculate"}, error="label not literal")
    success = turn("one", action={"action": "calculate"})
    success["calculation"] = {"answer": "synthetic"}
    write(directory / "a.json", trace("a", [failed, success, turn("one")]))
    result = analysis.analyze([directory], path)
    assert result["logged_tool_actions"] == {"calculate": 2}
    assert result["calculation_outcomes"] == {"label not literal": 1, "success": 1}
    assert result["confirmed_wrong"] == 1


def test_usage_is_only_a_known_subtotal_when_trace_or_usage_is_missing(tmp_path):
    path = evaluation(
        tmp_path,
        [
            {"question_id": "a", "correct": True, "status": "ANSWER"},
            {"question_id": "b", "correct": False, "status": "ERROR"},
        ],
    )
    directory = tmp_path / "traces"
    write(
        directory / "a.json",
        trace(
            "a",
            [turn("s")],
            tokens={"reader_input": 17, "reader_output": 8},
        ),
    )
    result = analysis.analyze([directory], path)
    assert result["reader_usage"]["input_tokens"] == {
        "known_subtotal": 17,
        "unknown_records": 1,
        "total": None,
    }
    assert result["cost_cny"] is None


def test_unknown_judge_with_no_completed_attempt_retains_unknown_usage(tmp_path):
    path = evaluation(
        tmp_path,
        [
            {
                "question_id": "a",
                "correct": None,
                "status": "ANSWER",
                "judge_attempts": [],
                "judge": {"input_tokens": None, "output_tokens": None},
            }
        ],
    )
    directory = tmp_path / "traces"
    write(directory / "a.json", trace("a", [turn("s")]))
    usage = analysis.analyze([directory], path)["judge_usage"]
    assert usage["logged_attempts"] == 0
    assert usage["unlogged_unknown_outcomes"] == 1
    assert usage["input_tokens"]["total"] is None


@pytest.mark.parametrize("value", [True, -1, 1.5, "20"])
def test_invalid_token_values_are_unknown_not_numbers(tmp_path, value):
    path = evaluation(
        tmp_path, [{"question_id": "a", "correct": True, "status": "ANSWER"}]
    )
    directory = tmp_path / "traces"
    write(directory / "a.json", trace("a", [], tokens={"reader_input": value}))
    result = analysis.analyze([directory], path)
    assert result["reader_usage"]["input_tokens"]["total"] is None


@pytest.mark.parametrize("kind", ["evaluation", "trace", "selected", "correct"])
def test_bad_denominators_duplicate_ids_and_nonboolean_verdicts_rejected(
    tmp_path, kind
):
    row = {"question_id": "a", "correct": True, "status": "ANSWER"}
    path = evaluation(tmp_path, [row, row] if kind == "evaluation" else [row])
    directory = tmp_path / "traces"
    write(directory / "a.json", trace("a", []))
    if kind == "trace":
        write(directory / "duplicate.json", trace("a", []))
    if kind == "selected":
        write(path, {"selected": 2, "results": [row]})
    if kind == "correct":
        write(path, {"selected": 1, "results": [{**row, "correct": 1}]})
    with pytest.raises(ValueError):
        analysis.analyze([directory], path)


def test_unknown_visibility_is_not_reported_as_no_loss(tmp_path):
    path = evaluation(
        tmp_path, [{"question_id": "a", "correct": True, "status": "ANSWER"}]
    )
    directory = tmp_path / "traces"
    write(
        directory / "a.json",
        trace("a", [turn("one"), {"context": "unsupported rendered format"}]),
    )
    visibility = analysis.analyze([directory], path)["source_visibility"]
    assert visibility["unknown_transition_count"] == 1
    assert visibility["rows"][0]["initial_sources_missing_from_final"] is None


def test_failure_message_detail_and_multiple_directories(tmp_path):
    path = evaluation(
        tmp_path,
        [
            {"question_id": "a", "correct": False, "status": "INVALID"},
            {"question_id": "b", "correct": True, "status": "ANSWER"},
        ],
    )
    a, b = tmp_path / "first", tmp_path / "second"
    write(
        a / "a.json",
        {
            "question_id": "a",
            "failure": {"message": json.dumps({"turns": [turn("s")]})},
        },
    )
    write(b / "b.json", trace("b", [turn("t")]))
    result = analysis.analyze([a, b], path)
    assert result["saved_traces"] == 2
    assert result["reader_call_counts"] == {1: 2}


def test_report_does_not_copy_answers_references_or_context_and_cannot_overwrite(
    tmp_path,
):
    marker = "do-not-publish-this-payload"
    path = evaluation(
        tmp_path,
        [
            {
                "question_id": "a",
                "correct": True,
                "status": "ANSWER",
                "hypothesis": marker,
                "reference": marker,
                "judge": {"raw": marker, "input_tokens": 3, "output_tokens": 1},
            }
        ],
    )
    directory = tmp_path / "traces"
    write(directory / "a.json", trace("a", [{"context": marker, "response": marker}]))
    original = path.read_bytes()
    result = analysis.analyze([directory], path)
    assert marker not in json.dumps(result)
    assert path.read_bytes() == original
    with pytest.raises(FileExistsError):
        analysis.main(
            [
                "--traces",
                str(directory),
                "--evaluation",
                str(path),
                "--output",
                str(path),
            ]
        )
    assert path.read_bytes() == original


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"selected": 1, "selected": 2, "results": []}')
    with pytest.raises(ValueError, match="Invalid JSON"):
        analysis.analyze([], path)
