"""A project is loaded, closed, then resumed through separate CLI invocations."""

import json

from dive_memory.cli import main


def test_casebook_to_historical_and_current_handoff(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DIVE_MODE", "evidence_only")
    db = str(tmp_path / "handoff.db")
    base = ["--db", db]
    assert main([*base, "casebook", "--project", "research"]) == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["curated"] is True

    assert (
        main(
            [
                *base,
                "brief",
                "--project",
                "research",
                "--as-of",
                "2026-10-10",
                "--format",
                "markdown",
            ]
        )
        == 0
    )
    historical = capsys.readouterr().out
    assert "422/500" in historical
    assert "451/500" not in historical

    assert main([*base, "brief", "--project", "research", "--format", "markdown"]) == 0
    current = capsys.readouterr().out
    assert "451/500" in current and "NOT_EVALUATED" in current
    assert "docs/oracle/" in current

    assert main([*base, "consult", "--project", "research", "R27 推广决定"]) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "ANSWER"
    assert "未采用" in rejected["answer"] and "不推广" in rejected["answer"]
    assert rejected["citations"]

    assert main([*base, "consult", "--project", "other", "R27 推广决定"]) == 0
    isolated = json.loads(capsys.readouterr().out)
    assert isolated["status"] != "ANSWER"
    assert not isolated["citations"]
