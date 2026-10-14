"""The quality gate fails closed on missing data and unrounded regressions."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "quality_gate", Path(__file__).parents[1] / "tools/check_quality.py"
)
assert spec and spec.loader
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def fixture_report():
    return {
        "meta": {"branch_coverage": True},
        "files": {
            "src\\app.py": {
                "summary": {
                    "covered_lines": 8999,
                    "num_statements": 10000,
                    "covered_branches": 80,
                    "num_branches": 100,
                }
            }
        },
    }


GROUPS = {"app": {"include": ["src/app.py"], "line-min": 90, "branch-min": 80}}


def test_gate_checks_lines_and_branches_without_rounding():
    report = fixture_report()
    assert not quality.coverage_groups(report, GROUPS)["app"]["passed"]
    summary = report["files"]["src\\app.py"]["summary"]
    summary["covered_lines"] = 9000
    assert quality.coverage_groups(report, GROUPS)["app"]["passed"]
    summary["covered_branches"] = 79
    assert not quality.coverage_groups(report, GROUPS)["app"]["passed"]


@pytest.mark.parametrize("missing", ["file", "branches", "denominator"])
def test_gate_cannot_pass_by_omitting_data(missing):
    report = fixture_report()
    if missing == "file":
        report["files"] = {}
    elif missing == "branches":
        report["meta"]["branch_coverage"] = False
    else:
        report["files"]["src\\app.py"]["summary"]["num_branches"] = 0
    with pytest.raises(ValueError):
        quality.coverage_groups(report, GROUPS)


def test_existing_evidence_is_not_overwritten(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("previous failure", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        quality.main(["--output", str(tmp_path)])
    assert exc.value.code == 2
    assert marker.read_text(encoding="utf-8") == "previous failure"


def test_wildcard_cannot_hide_an_unmeasured_source_file(tmp_path):
    (tmp_path / "src").mkdir()
    for name in ("app.py", "new_module.py"):
        (tmp_path / "src" / name).write_text("pass\n", encoding="utf-8")
    groups = {"app": {**GROUPS["app"], "include": ["src/*.py"]}}
    with pytest.raises(ValueError, match="new_module.py"):
        quality.coverage_groups(fixture_report(), groups, root=tmp_path)
