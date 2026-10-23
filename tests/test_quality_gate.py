"""The quality gate fails closed on missing data and unrounded regressions."""

import hashlib
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


@pytest.mark.parametrize(
    "name",
    [
        "src/dive_memory/module.py",
        "src/dive_memory/static/app.js",
        "src/dive_memory/static/app.css",
        "src/dive_memory/static/index.html",
        "src/dive_memory/static/icon.svg",
        "src/dive_memory/data/casebook.json",
        "tests/test_feature.py",
        "tools/verify_workbench.cjs",
        "tools/windows/start.ps1",
        "requirements/runtime.lock",
        ".github/workflows/extra.yaml",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "migrations/008_next.sql",
        "ops/docker/Dockerfile",
        "ops/docker/compose.yml",
        "ops/kubernetes/deployment.yaml",
        "ops/postgres/backup.sh",
    ],
)
def test_manifest_tracks_added_changed_and_deleted_delivery_files(tmp_path, name):
    before = quality.source_manifest(root=tmp_path)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"first\n")
    added = quality.source_manifest(root=tmp_path)
    assert set(added) == {name}
    assert added != before
    path.write_bytes(b"second\n")
    changed = quality.source_manifest(root=tmp_path)
    assert changed != added
    path.unlink()
    assert quality.source_manifest(root=tmp_path) == before


def test_manifest_normalizes_text_line_endings_and_keeps_binary_bytes(tmp_path):
    path = tmp_path / "src/dive_memory/static/app.js"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first\r\nsecond\r\n")
    windows = quality.source_manifest(root=tmp_path)
    path.write_bytes(b"first\nsecond\n")
    assert quality.source_manifest(root=tmp_path) == windows
    binary = path.with_name("icon.png")
    content = b"\x89PNG\r\n\x1a\n"
    binary.write_bytes(content)
    manifest = quality.source_manifest(root=tmp_path)
    assert (
        manifest[binary.relative_to(tmp_path).as_posix()]
        == hashlib.sha256(content).hexdigest()
    )


def test_manifest_ignores_local_outputs_caches_and_experiment_evidence(tmp_path):
    names = (
        "src/dive_memory/static/app.js.map",
        "src/dive_memory/local.db",
        "src/dive_memory/__pycache__/cached.py",
        "tests/.pytest_cache/generated.py",
        "tools/node_modules/third_party.js",
        "artifacts/source.py",
        "eval/reports/run/source.py",
        "eval/external/data.json",
        "ops/oracle_experiment.py",
        "ops/postgres/local.db",
        "dist/source.py",
        "node_modules/module/package.json",
        ".env",
        "README.md",
    )
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unrelated\n")
    assert quality.source_manifest(root=tmp_path) == {}


def test_manifest_default_root_remains_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    (tmp_path / "package.json").write_bytes(b"{}\n")
    assert quality.source_manifest() == quality.source_manifest(root=tmp_path)


def test_default_manifest_detects_frontend_change_during_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    for name in ("pyproject.toml", ".github/workflows/quality.yml"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"config\n")
    asset = tmp_path / "src/dive_memory/static/app.js"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"first\n")
    before = quality.source_manifest()
    asset.write_bytes(b"second\n")
    assert quality.source_manifest() != before
