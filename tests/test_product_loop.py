"""Personal workflow survives retries, failures, isolation, restart and backup."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.web import create_workbench


@pytest.fixture
def app(tmp_path):
    runtime = MemoryApplication(str(tmp_path / "workflow.db"))
    yield runtime
    runtime.close()


def record(app, namespace="legacy", value="first", key="progress", token="r", **kw):
    return app.record(
        namespace,
        ProjectRecord(key, value, "note"),
        idempotency_key=token,
        **kw,
    )["memory_ids"][0]


def test_project_catalogue_migrates_legacy_and_survives_restart(tmp_path):
    path = str(tmp_path / "legacy.db")
    app = MemoryApplication(path)
    assert app.projects() == []
    record(app)
    assert app.projects()[0]["namespace"] == "legacy"
    assert app.projects()[0]["record_count"] == 1
    empty = app.project_details("new")
    assert empty["name"] == "new" and empty["tasks"] == []
    created = app.create_project("论文", "完成实验", "create")
    assert app.create_project("论文", "完成实验", "create") == created
    with pytest.raises(ValueError, match="different project"):
        app.create_project("another", "", "create")
    project = created["namespace"]
    app.update_project(project, "论文第二阶段", "整理结果")
    app.update_project("legacy", "旧项目", "继续")
    app.close()
    app = MemoryApplication(path)
    try:
        assert {p["name"] for p in app.projects()} == {"旧项目", "论文第二阶段"}
        assert app.project_details(project)["goal"] == "整理结果"
    finally:
        app.close()


@pytest.mark.parametrize(
    "name,goal",
    [
        ("", ""),
        ("a" * 81, ""),
        ("line\nnext", ""),
        ("line\rnext", ""),
        ("valid", "g" * 1001),
        (None, ""),
    ],
)
def test_invalid_project_cannot_be_partially_created(app, name, goal):
    with pytest.raises(ValueError):
        app.create_project(name, goal, "bad")
    assert app.projects() == []


@pytest.mark.parametrize(
    "operation",
    [
        lambda app: app.project_details(" "),
        lambda app: app.create_project("valid", "", ""),
        lambda app: app.add_task("p", " ", "t"),
        lambda app: app.add_task("p", "t" * 501, "t"),
        lambda app: app.checkpoint("p", "", "next", "c"),
        lambda app: app.checkpoint("p", "note", "n" * 501, "c"),
    ],
)
def test_invalid_workflow_does_not_write(app, operation):
    with pytest.raises(ValueError):
        operation(app)
    assert app.projects() == []


def test_tasks_isolation_retry_completion_reopen_and_stale_edits(app):
    initial = app.add_task("a", "read sources", "t")
    task = initial["tasks"][0]
    assert app.add_task("a", "read sources", "t") == initial
    app.add_task("b", "different project", "t")
    with pytest.raises(ValueError, match="different task"):
        app.add_task("a", "other", "t")
    with pytest.raises(KeyError):
        app.update_task("b", task["id"], "read", True, 1)
    done = app.update_task("a", task["id"], "read sources", True, 1)
    assert done["tasks"][0]["completed"] == 1
    assert app.update_task("a", task["id"], "read sources", True, 1) == done
    with pytest.raises(ValueError, match="其他页面"):
        app.update_task("a", task["id"], "stale", False, 1)
    reopened = app.update_task("a", task["id"], "read another", False, 2)
    assert reopened["tasks"][0]["revision"] == 3
    assert reopened["tasks"][0]["completed"] == 0
    assert app.add_task("a", "read sources", "t") == reopened
    assert app.project_details("b")["tasks"][0]["title"] == "different project"
    assert all(p["open_tasks"] == 1 for p in app.projects())


@pytest.mark.parametrize(
    "completed,revision", [(1, 1), (False, True), (False, 0), (False, "1")]
)
def test_invalid_task_state(app, completed, revision):
    task = app.add_task("p", "title", "t")["tasks"][0]
    with pytest.raises(ValueError):
        app.update_task("p", task["id"], "title", completed, revision)
    assert app.project_details("p")["tasks"] == [task]


def test_checkpoint_retry_atomicity_and_bounded_history(app, monkeypatch):
    original = app.workspace.add_task

    def fail(*args):
        original(*args)
        raise sqlite3.OperationalError("simulated storage failure")

    monkeypatch.setattr(app.workspace, "add_task", fail)
    with pytest.raises(sqlite3.OperationalError):
        app.checkpoint("p", "note", "next", "c")
    assert app.projects() == []
    assert app.project_details("p")["checkpoints"] == []
    assert app.project_details("p")["tasks"] == []
    monkeypatch.setattr(app.workspace, "add_task", original)
    saved = app.checkpoint("p", "note", "next", "c")
    assert app.checkpoint("p", "note", "next", "c") == saved
    with pytest.raises(ValueError, match="different checkpoint"):
        app.checkpoint("p", "changed", "next", "c")
    for i in range(22):
        app.checkpoint("p", f"session {i}", "", str(i))
    details = app.project_details("p")
    assert len(details["tasks"]) == 1
    assert len(details["checkpoints"]) == 20
    assert details["checkpoints"][0]["note"] == "session 21"


def test_current_handoff_includes_work_without_leaking_into_history(app):
    app.update_project("p", "个人论文", "完成实验")
    record(app, "p", observed_at="2026-01-01")
    task = app.add_task("p", "finished", "t")["tasks"][0]
    app.update_task("p", task["id"], "finished", True, 1)
    app.checkpoint("p", "已完成初稿", "明天检查", "c")
    current = app.brief("p")
    for text in (
        "个人论文",
        "完成实验",
        "已完成初稿",
        "明天检查",
        "已完成：",
        "finished",
    ):
        assert text in current["markdown"]
    historical = app.brief("p", as_of="2026-01-02")
    assert "workspace" not in historical
    assert "明天检查" not in historical["markdown"]
    assert "first" in historical["markdown"]
    assert "继续工作的安排" in app.brief("empty")["markdown"]


def test_api_backup_restores_full_loop_and_erasure(tmp_path):
    app = MemoryApplication(str(tmp_path / "source.db"))
    with TestClient(create_workbench(app, access_token="local-test")) as client:
        assert client.get("/api/backup").status_code == 401
        assert client.get("/api/projects").status_code == 401
        assert client.post("/api/tasks", json={}).status_code == 401
        client.headers["Authorization"] = "Bearer local-test"
        created = client.post(
            "/api/projects",
            json={"name": "毕业论文", "goal": "初稿", "request_id": "p"},
        )
        assert created.status_code == 200
        project = created.json()["namespace"]
        assert (
            client.put(
                "/api/project",
                params={"project": project},
                json={"name": "论文", "goal": "终稿", "request_id": "edit"},
            ).status_code
            == 200
        )
        assert client.get("/api/projects").json()["projects"][0]["name"] == "论文"
        task = client.post(
            "/api/tasks",
            json={"project": project, "title": "阅读资料", "request_id": "t"},
        ).json()["tasks"][0]
        update = {
            "project": project,
            "title": "阅读资料",
            "completed": True,
            "revision": 1,
        }
        assert client.put("/api/tasks/" + task["id"], json=update).status_code == 200
        assert (
            client.put(
                "/api/tasks/" + task["id"], json={**update, "completed": "yes"}
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/api/tasks/" + task["id"], json={**update, "project": "other"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/projects", json={"name": " ", "request_id": "bad"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/checkpoints",
                json={
                    "project": project,
                    "note": "资料看完了",
                    "next_step": "开始撰写",
                    "request_id": "c",
                },
            ).status_code
            == 200
        )
        old = record(app, project, observed_at="2026-01-01")
        record(
            app,
            project,
            value="second",
            token="r2",
            observed_at="2026-01-02",
            replaces=old,
        )
        erased = record(app, project, value="private", key="erase", token="erase")
        app.forget(project, erased)
        before = client.get("/api/project", params={"project": project}).json()
        response = client.get("/api/backup")
        assert response.status_code == 200
        assert response.content.startswith(b"SQLite format 3")
        restored_path = tmp_path / "restored.db"
        restored_path.write_bytes(response.content)
    restored = MemoryApplication(str(restored_path))
    try:
        assert restored.project_details(project) == before
        assert restored.lookup(project, "progress")["value"] == "second"
        assert (
            restored.lookup(project, "progress", as_of="2026-01-01")["value"] == "first"
        )
        assert len(restored.snapshot(project)["timeline"]) == 2
        with pytest.raises(KeyError):
            restored.source(project, erased)
        restored.service.rebuild_projections(project)
        assert restored.lookup(project, "erase")["reason"] == "missing_record"
        assert restored.project_details(project) == before
        assert (
            restored.service.store.db.execute("PRAGMA integrity_check").fetchone()[0]
            == "ok"
        )
    finally:
        restored.close()


def test_backup_failure_removes_temporary_file(app, tmp_path, monkeypatch):
    import dive_memory.web as web

    real_temporary = web.tempfile.NamedTemporaryFile
    paths = []

    def temporary(**kwargs):
        handle = real_temporary(dir=tmp_path, **kwargs)
        paths.append(web.Path(handle.name))
        return handle

    def fail(_path):
        raise sqlite3.OperationalError("backup failed")

    monkeypatch.setattr(web.tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(app, "backup", fail)
    with TestClient(create_workbench(app), raise_server_exceptions=False) as client:
        assert client.get("/api/backup").status_code == 500
    assert paths and all(not path.exists() for path in paths)
