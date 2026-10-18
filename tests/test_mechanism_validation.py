"""Experiments retain failures, paired denominators and restart semantics."""

import json
from pathlib import Path

import pytest

from dive_memory import mechanism_validation as experiment


def fixture_subset(tmp_path):
    source = Path(__file__).parents[1] / "eval/mechanisms/stories-v1.jsonl"
    rows = experiment.load_fixtures(source)
    subset = [
        next(row for row in rows if row["family"] == family)
        for family in ("updates", "roles", "forget")
    ]
    path = tmp_path / "stories.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in subset), encoding="utf-8")
    return path


def test_mechanisms_reopen_persistent_stores_and_keep_strong_baseline(tmp_path):
    fixtures = fixture_subset(tmp_path)
    output = tmp_path / "result"
    result = experiment.run(fixtures, output)
    assert result["arms"]["memory"] == {"correct": 12, "total": 12, "errors": 0}
    assert result["arms"]["event_scan"] == result["arms"]["memory"]
    assert result["paired_vs_memory"]["without_forgetting"]["memory_only"] == 2
    assert result["paired_vs_memory"]["without_source_gate"]["memory_only"] == 2
    assert result["paired_vs_memory"]["without_updates"]["memory_only"] == 1
    assert (output / "fixtures.jsonl").read_bytes() == fixtures.read_bytes()
    with pytest.raises(FileExistsError):
        experiment.run(fixtures, output)


def test_failed_tasks_are_wrong_even_when_expected_is_abstention(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("injected error")

    monkeypatch.setattr(experiment, "baseline", fail)
    result = experiment.run(fixture_subset(tmp_path), tmp_path / "failed")
    assert result["arms"]["event_scan"] == {"correct": 0, "total": 12, "errors": 12}
    rows = json.loads((tmp_path / "failed/results.json").read_text())["rows"]
    null_expected = [
        row for row in rows if row["expected"] is None and row["arm"] == "event_scan"
    ]
    assert null_expected and all(not row["correct"] for row in null_expected)
    with pytest.raises(ValueError, match="denominator"):
        experiment.summarize(rows[:-1], 12)


def test_fixture_duplicate_task_or_unknown_delete_fails_before_running(tmp_path):
    path = fixture_subset(tmp_path)
    rows = experiment.load_fixtures(path)
    rows[0]["tasks"].append(rows[0]["tasks"][0])
    path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="task IDs"):
        experiment.run(path, tmp_path / "duplicate")
    assert not (tmp_path / "duplicate").exists()
    rows[0]["tasks"].pop()
    rows[0]["events"].append({"id": "invalid", "delete": "unknown", "at": "2026-01-20"})
    path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="delete"):
        experiment.load_fixtures(path)
