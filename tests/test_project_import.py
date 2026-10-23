"""Folder snapshots initialize useful, isolated and recoverable project progress."""

import copy
import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from dive_memory.application.agent_tools import ProjectAdapter
from dive_memory.application.project_import import analyze_folder, safe_path
from dive_memory.application.runtime import MemoryApplication
from dive_memory.web import create_workbench


def folder():
    return {
        "name": "论文项目",
        "paths": [
            "README.md",
            "docs/进度.md",
            "src/main.py",
            ".env",
            "node_modules/pkg/a.js",
        ],
        "documents": [
            {"path": "README.md", "content": "# 论文\n比较两种误差指标。\n"},
            {
                "path": "docs/进度.md",
                "content": "# 当前进度\n已完成基线实验。\n- [x] 整理样本\n- [ ] 比较 MAE\n- [ ] 撰写初稿\n",
            },
        ],
    }


@pytest.fixture
def app(tmp_path):
    runtime = MemoryApplication(str(tmp_path / "import.db"))
    yield runtime
    runtime.close()


def test_preview_is_offline_sourced_and_does_not_claim_verified_completion(app):
    with TestClient(create_workbench(app)) as client:
        response = client.post("/api/project-import/preview", json=folder())
        assert response.status_code == 200
        result = response.json()
        assert app.projects() == []
        assert result["paths"] == ["README.md", "docs/进度.md", "src/main.py"]
        assert result["tasks"] == [
            {"title": "比较 MAE", "source": "docs/进度.md:4"},
            {"title": "撰写初稿", "source": "docs/进度.md:5"},
        ]
        assert result["completed"][0]["title"] == "整理样本"
        assert "已完成基线实验" in result["note"]
        assert "尚未运行或验证" in result["note"]
        assert "content" not in result["sources"][0]
        assert all(len(source["sha256"]) == 64 for source in result["sources"])


def test_import_persists_sources_reviewed_progress_tasks_handoff_and_backup(
    app, tmp_path
):
    selected = folder()
    created = app.create_project(
        "论文",
        "完成初稿",
        "one",
        folder=selected,
        progress_note="已核对：基线完成，比较实验尚未开始。",
        next_step="比较 MAE",
        tasks=["比较 MAE"],
    )
    project = created["namespace"]
    assert [task["title"] for task in created["tasks"]] == ["比较 MAE"]
    assert created["checkpoints"][0]["note"].startswith("已核对")
    assert created["initialization"]["folder_name"] == "论文项目"
    assert created["initialization"]["file_count"] == 3
    assert "已核对" in ProjectAdapter(app).context(project)["checkpoints"][0]["note"]
    state = app.snapshot(project)
    assert len(state["memories"]) == 3
    source = next(
        m
        for m in state["memories"]
        if m["structured_content"].get("source_path") == "docs/进度.md"
    )
    data = source["structured_content"]
    content = selected["documents"][1]["content"]
    assert data["source_excerpt"] == content
    assert data["source_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert app.source(project, source["id"])["events"]
    assert "已核对" in app.brief(project)["markdown"]
    assert app.search_project(project, "基线")["checkpoints"]
    other = app.create_project("另一项目", "", "two")
    assert other["initialization"] is None
    assert app.snapshot(other["namespace"])["memories"] == []
    assert app.project_details(other["namespace"])["checkpoints"] == []
    backup = tmp_path / "backup.db"
    app.backup(str(backup))
    restored = MemoryApplication(str(backup))
    try:
        assert restored.project_details(project) == created
        assert restored.snapshot(project)["memories"] == state["memories"]
    finally:
        restored.close()


def test_identical_retry_after_restart_does_not_duplicate_and_changed_import_is_rejected(
    tmp_path,
):
    path = str(tmp_path / "retry.db")
    app = MemoryApplication(path)
    created = app.create_project("论文", "", "one", folder=folder())
    app.close()
    app = MemoryApplication(path)
    try:
        assert app.create_project("论文", "", "one", folder=folder()) == created
        assert len(app.snapshot(created["namespace"])["memories"]) == 3
        changed = folder()
        changed["documents"][0]["content"] += "变更"
        for changes in (
            {"folder": changed},
            {"folder": folder(), "progress_note": "修改"},
            {},
        ):
            with pytest.raises(ValueError, match="同一请求编号"):
                app.create_project("论文", "", "one", **changes)
    finally:
        app.close()


def test_initialization_failure_rolls_back_all_project_data(app, monkeypatch):
    original = app.record
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(app, "record", fail)
    with pytest.raises(sqlite3.OperationalError):
        app.create_project("论文", "", "one", folder=folder())
    assert app.projects() == []
    for table in (
        "events",
        "memories",
        "app_projects",
        "app_tasks",
        "app_checkpoints",
        "app_project_imports",
    ):
        assert (
            app.service.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            == 0
        )
    monkeypatch.setattr(app, "record", original)
    assert app.create_project("论文", "", "one", folder=folder())["initialization"]


@pytest.mark.parametrize(
    "path",
    ["../a", "/etc/passwd", "C:\\a", "a/../b", "a/./b", "a//b", "a\n.txt", ".", ""],
)
def test_only_relative_normalized_paths_are_accepted(path):
    with pytest.raises(ValueError, match="路径"):
        safe_path(path)


def test_sensitive_content_and_code_examples_are_not_progress(app):
    selected = folder()
    selected["documents"][0]["content"] = 'api_key="sk-this-is-a-secret-key-value"'
    selected["documents"][1]["content"] = (
        "```md\n- [x] 示例而非实际进度\n```\n仅有源代码。"
    )
    result = analyze_folder(selected)
    assert len(result["sources"]) == 1
    assert not result["completed"] and not result["tasks"]
    assert "未找到明确的进度记录" in result["note"]
    created = app.create_project("论文", "", "one", folder=selected)
    assert "sk-this" not in str(app.snapshot(created["namespace"]))


def test_blank_checkboxes_do_not_block_default_project_initialization(app):
    selected = folder()
    selected["documents"][1]["content"] = (
        "# 当前进度\n- [ ]    \n- [x] \t \n- [ ] 比较 MAE\n"
    )
    result = analyze_folder(selected)
    assert result["tasks"] == [{"title": "比较 MAE", "source": "docs/进度.md:4"}]
    assert result["completed"] == []
    assert result["next_step"] == "比较 MAE"
    created = app.create_project("论文", "", "blank-checkboxes", folder=selected)
    assert [task["title"] for task in created["tasks"]] == ["比较 MAE"]
    assert created["checkpoints"][0]["next_step"] == "比较 MAE"


@pytest.mark.parametrize(
    "example",
    [
        "````md\n~~~\n- [ ] 示例任务\n````",
        "````md\n```\n- [ ] 示例任务\n````",
        "````md\n```md\n- [ ] 示例任务\n```\n````",
        "```md\n``` trailing text\n- [ ] 示例任务\n```",
        "~~~md\n```\n- [ ] 示例任务\n~~~~",
        "~~~md\n~~\n- [ ] 示例任务\n~~~ trailing text\n~~~",
        "```md\n- [ ] 示例任务\n````",
    ],
)
def test_code_fences_close_only_with_matching_plain_delimiters(example):
    selected = {
        "name": "项目",
        "paths": ["README.md"],
        "documents": [{"path": "README.md", "content": example + "\n- [ ] 真实待办\n"}],
    }
    result = analyze_folder(selected)
    assert result["tasks"] == [
        {
            "title": "真实待办",
            "source": f"README.md:{len(example.splitlines()) + 1}",
        }
    ]
    assert "示例任务" not in result["note"]


@pytest.mark.parametrize(
    "change",
    [
        {"paths": ["README.md"] * 2001},
        {"documents": [{"path": "README.md", "content": "a" * 12001}]},
        {"documents": [{"path": "not-selected.md", "content": "text"}]},
        {"documents": [{"path": "README.md", "content": "x"}] * 2},
        {"name": ""},
        {"skipped": -1},
    ],
)
def test_invalid_import_has_no_partial_project(app, change):
    selected = {**folder(), **copy.deepcopy(change)}
    with pytest.raises(ValueError):
        app.create_project("论文", "", "one", folder=selected)
    assert app.projects() == []


@pytest.mark.parametrize("invalid", [None, [], "folder", 1])
def test_folder_analysis_rejects_non_object_input(invalid):
    with pytest.raises(ValueError, match="对象"):
        analyze_folder(invalid)


@pytest.mark.parametrize("invalid", [None, [], "document", 1])
def test_non_object_documents_have_clear_boundary_errors_without_partial_writes(
    app, invalid
):
    selected = {**folder(), "documents": [invalid]}
    with pytest.raises(ValueError, match="对象"):
        app.create_project("论文", "", "non-object-document", folder=selected)
    assert app.projects() == []


def test_api_supports_reviewed_edits_and_preserves_existing_create_and_update(app):
    with TestClient(create_workbench(app), raise_server_exceptions=False) as client:
        body = {
            "name": "论文",
            "goal": "",
            "request_id": "one",
            "folder": folder(),
            "progress_note": "核对后的进度",
            "next_step": "",
            "tasks": [],
        }
        created = client.post("/api/projects", json=body)
        assert created.status_code == 200
        data = created.json()
        assert data["tasks"] == []
        assert data["checkpoints"][0]["note"] == "核对后的进度"
        assert client.post("/api/projects", json=body).json() == data
        body["tasks"] = ["凭空增加的待办"]
        assert client.post("/api/projects", json=body).status_code == 400
        plain = {"name": "空项目", "goal": "", "request_id": "plain"}
        assert client.post("/api/projects", json=plain).json()["initialization"] is None
        updated = client.put(
            "/api/project",
            params={"project": data["namespace"]},
            json={**plain, "name": "改名"},
        )
        assert updated.status_code == 200
        assert updated.json()["initialization"] == data["initialization"]
        assert (
            client.post(
                "/api/project-import/preview",
                json={**folder(), "paths": ["../private"]},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/project-import/preview",
                json={
                    **folder(),
                    "documents": [{"path": "README.md", "content": "x" * 12001}],
                },
            ).status_code
            == 422
        )


def test_import_endpoints_share_local_access_and_origin_boundary(app):
    with TestClient(create_workbench(app, access_token="local-only")) as client:
        assert (
            client.post("/api/project-import/preview", json=folder()).status_code == 401
        )
        assert (
            client.post(
                "/api/project-import/preview",
                json=folder(),
                headers={
                    "authorization": "Bearer local-only",
                    "origin": "https://other.example",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/project-import/preview",
                json=folder(),
                headers={"authorization": "Bearer local-only"},
            ).status_code
            == 200
        )
