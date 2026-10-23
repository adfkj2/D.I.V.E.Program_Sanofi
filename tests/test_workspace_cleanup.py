"""Deletion boundaries for disposable caches and archived pytest fixtures."""

import importlib.util
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

spec = importlib.util.spec_from_file_location(
    "workspace_cleanup", Path(__file__).parents[1] / "tools/clean_workspace.py"
)
assert spec and spec.loader
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    return tmp_path


def write(root, name, content="temporary"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_preview_is_read_only_and_ignores_user_data(repository):
    write(repository, ".ruff_cache/index")
    protected = [
        "memory.db",
        ".venv/model",
        "eval/reports/failed.json",
        "dist/release.whl",
        "docs/draft.md",
        ".workbench/memory.db",
    ]
    for name in protected:
        write(repository, name)
    plan = cleanup.plan_cleanup(repository)
    assert [t["path"] for t in plan["targets"]] == [".ruff_cache"]
    assert all((repository / name).exists() for name in protected)
    assert (repository / ".ruff_cache/index").exists()


def test_apply_archives_database_before_removing_pytest_temporary(repository):
    write(repository, ".pytest-old/db.sqlite", "saved synthetic database")
    write(repository, "build/lib/old.py")
    plan = cleanup.plan_cleanup(repository)
    result = cleanup.apply_cleanup(plan, repository / "eval/reports/cleanup")
    assert result["errors"] == []
    assert set(result["deleted"]) == {".pytest-old", "build"}
    assert not (repository / ".pytest-old").exists()
    with ZipFile(repository / result["archive"]) as bundle:
        assert bundle.read(".pytest-old/db.sqlite") == b"saved synthetic database"


def test_tracked_cache_file_is_never_deleted(repository):
    path = write(repository, "build/valuable.txt")
    subprocess.run(["git", "add", "build/valuable.txt"], cwd=repository, check=True)
    plan = cleanup.plan_cleanup(repository)
    assert plan["targets"] == [] and plan["blocked"]
    cleanup.apply_cleanup(plan, repository / "eval/reports/cleanup")
    assert path.read_text() == "temporary"


def test_changed_preview_does_not_delete_anything(repository):
    path = write(repository, ".pytest-old/notes")
    plan = cleanup.plan_cleanup(repository)
    path.write_text("new result")
    with pytest.raises(ValueError, match="changed"):
        cleanup.apply_cleanup(plan, repository / "eval/reports/cleanup")
    assert path.read_text() == "new result"


def test_existing_records_and_output_outside_repository_are_rejected(
    repository, tmp_path_factory
):
    path = write(repository, ".ruff_cache/index")
    plan = cleanup.plan_cleanup(repository)
    with pytest.raises(ValueError, match="eval/reports"):
        cleanup.apply_cleanup(plan, tmp_path_factory.mktemp("outside"))
    output = repository / "eval/reports/existing"
    output.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        cleanup.apply_cleanup(plan, output)
    assert path.exists()


def test_nonempty_deploy_directory_is_preserved(repository):
    path = write(repository, "deploy/user-config")
    assert cleanup.plan_cleanup(repository)["targets"] == []
    assert path.exists()


def test_symlink_target_is_blocked(repository, tmp_path_factory):
    outside = tmp_path_factory.mktemp("protected")
    protected = write(outside, "user.txt")
    try:
        (repository / "build").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create directory symlinks")
    plan = cleanup.plan_cleanup(repository)
    assert not plan["targets"] and plan["blocked"]
    assert protected.exists()


def test_unreadable_cache_is_blocked_instead_of_reported_empty(repository, monkeypatch):
    write(repository, ".pytest-private/db.sqlite")
    original = cleanup.os.walk

    def unreadable(target, **kwargs):
        if target.name == ".pytest-private":
            kwargs["onerror"](PermissionError("private"))
        return original(target, **kwargs)

    monkeypatch.setattr(cleanup.os, "walk", unreadable)
    plan = cleanup.plan_cleanup(repository)
    assert plan["targets"] == []
    assert plan["blocked"] == [{"path": ".pytest-private", "reason": "PermissionError"}]


def test_case_only_rename_cannot_bypass_tracked_file_protection(repository):
    path = write(repository, "build/note.txt", "tracked source")
    subprocess.run(["git", "add", "build/note.txt"], cwd=repository, check=True)
    renamed = path.with_name("Note.txt")
    path.rename(renamed)
    plan = cleanup.plan_cleanup(repository)
    assert not plan["targets"] and plan["blocked"]
    assert renamed.read_text() == "tracked source"


def test_write_between_deletions_preserves_the_changed_target(repository, monkeypatch):
    write(repository, ".pytest-a/old")
    protected = write(repository, ".pytest-b/old")
    plan = cleanup.plan_cleanup(repository)
    original = cleanup.shutil.rmtree

    def remove_then_write(path):
        original(path)
        if path.name == ".pytest-a":
            protected.write_text("new result after archive verification")

    monkeypatch.setattr(cleanup.shutil, "rmtree", remove_then_write)
    result = cleanup.apply_cleanup(plan, repository / "eval/reports/cleanup")
    assert result["deleted"] == [".pytest-a"] and result["errors"]
    assert protected.read_text() == "new result after archive verification"
