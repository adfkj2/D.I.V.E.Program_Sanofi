"""Agent contracts with scripted models; no provider score or live-model claim."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from dive_memory.agent.engine import AgentEngine
from dive_memory.agent.protocols import ModelError
from dive_memory.agent.transport import CompatibleModel
from dive_memory.application.assistant import Assistant, ProjectAdapter
from dive_memory.application.records import ProjectRecord
from dive_memory.application.runtime import MemoryApplication
from dive_memory.web import create_workbench


def call(name, **arguments):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        assert tools
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result(messages) if callable(result) else result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for key in ("DIVE_AGENT_ENDPOINT", "DIVE_AGENT_MODEL", "DIVE_AGENT_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    app = MemoryApplication(str(tmp_path / "agent.db"))
    app.update_project("p", "论文实验", "比较误差")
    yield app
    app.close()


def propose(*actions):
    return call("propose_changes", summary="准备保存，请确认。", actions=list(actions))


def test_agent_reads_evidence_before_citing(runtime):
    runtime.record("p", ProjectRecord("方法", "比较两种误差"), idempotency_key="source")
    memory_id = runtime.snapshot("p")["memories"][0]["id"]
    model = Scripted(
        call("search_memory", query="方法"),
        call("read_source", memory_id=memory_id),
        call("respond", message="已找到实验方法。", citations=[memory_id]),
    )
    agent = Assistant(runtime, model)
    result = agent.chat("p", "上次方法是什么？", "r1")
    assert result["status"] == "completed"
    assert result["citations"] == [memory_id]
    assert len(result["trace"]) == 3
    assert len(agent.history("p")) == 1
    assert agent.history("other") == []
    assert "fingerprint" not in result


def test_confirmed_batch_closes_loop_and_is_idempotent(runtime):
    model = Scripted(
        propose(
            {
                "kind": "save_record",
                "key": "误差定义",
                "value": "使用 MAE",
                "category": "decision",
                "disposition": "confirmed",
            },
            {"kind": "add_task", "title": "运行对比"},
            {"kind": "save_checkpoint", "note": "定义已确认", "next_step": "核对输出"},
        )
    )
    agent = Assistant(runtime, model)
    result = agent.chat("p", "记住误差定义，安排对比，并保存交接", "batch")
    assert result["status"] == "pending"
    assert runtime.snapshot("p")["memories"] == []
    assert runtime.project_details("p")["tasks"] == []
    assert agent.chat("p", result["prompt"], "batch") == result
    with pytest.raises(ValueError, match="不同内容"):
        agent.chat("p", "changed", "batch")
    with pytest.raises(ValueError, match="上一份"):
        agent.chat("p", "other", "another")
    with pytest.raises(KeyError):
        agent.decide("other", result["id"], True)
    applied = agent.decide("p", result["id"], True)
    assert applied["status"] == "applied"
    assert len(applied["receipts"]) == 3
    assert all(r["verified"] for r in applied["receipts"])
    assert agent.decide("p", result["id"], True) == applied
    assert len(runtime.project_details("p")["tasks"]) == 2
    assert runtime.lookup("p", "误差定义")["value"] == "使用 MAE"
    source = runtime.source("p", runtime.snapshot("p")["memories"][0]["id"])
    assert source["memory"]["structured_content"]["source_excerpt"] == result["prompt"]
    # Conversation and approval survive a new controller and database backup.
    assert Assistant(runtime).history("p")[0]["status"] == "applied"


def test_update_task_and_record_keep_history(runtime):
    runtime.add_task("p", "运行对比", "t1")
    task = runtime.project_details("p")["tasks"][0]
    runtime.record("p", ProjectRecord("方法", "旧定义"), idempotency_key="old")
    memory_id = runtime.snapshot("p")["memories"][0]["id"]
    agent = Assistant(
        runtime,
        Scripted(
            propose(
                {
                    "kind": "update_task",
                    "task_id": task["id"],
                    "title": "完成对比",
                    "completed": True,
                    "revision": 1,
                },
                {
                    "kind": "save_record",
                    "key": "方法",
                    "value": "新定义",
                    "replaces": memory_id,
                },
            )
        ),
    )
    result = agent.chat("p", "已完成对比，更新定义", "update")
    assert result["status"] == "pending"
    agent.decide("p", result["id"], True)
    assert runtime.project_details("p")["tasks"][0]["completed"] == 1
    assert runtime.lookup("p", "方法")["value"] == "新定义"
    assert len(runtime.snapshot("p")["timeline"]) == 2


def test_stale_proposal_cancel_and_transaction_rollback(runtime, monkeypatch):
    agent = Assistant(
        runtime, Scripted(propose({"kind": "add_task", "title": "first"}))
    )
    result = agent.chat("p", "安排任务", "stale")
    runtime.add_task("p", "外部改动", "external")
    with pytest.raises(ValueError, match="资料已变化"):
        agent.decide("p", result["id"], True)
    assert agent.decide("p", result["id"], False)["status"] == "cancelled"
    assert agent.decide("p", result["id"], False)["status"] == "cancelled"
    agent.model = Scripted(
        propose(
            {"kind": "add_task", "title": "rollback"},
            {"kind": "save_checkpoint", "note": "fail"},
        )
    )
    result = agent.chat("p", "安排并保存", "rollback")

    def fail(*args):
        raise ValueError("disk test failure")

    monkeypatch.setattr(runtime, "checkpoint", fail)
    with pytest.raises(ValueError, match="disk test"):
        agent.decide("p", result["id"], True)
    assert [t["title"] for t in runtime.project_details("p")["tasks"]] == ["外部改动"]
    assert agent.history("p")[-1]["status"] == "pending"


def test_unconfigured_errors_and_restart(runtime):
    agent = Assistant(runtime)
    with pytest.raises(ValueError, match="连接模型"):
        agent.chat("p", "hi", "r")
    agent.model = Scripted(ModelError("模型认证失败"))
    result = agent.chat("p", "hi", "r")
    assert result["status"] == "error"
    assert "认证失败" in result["message"]
    with pytest.raises(ValueError, match="没有待确认"):
        agent.decide("p", result["id"], True)
    stored = agent._get("p", result["id"])
    stored["status"] = "running"
    agent._save(stored)
    restored = Assistant(runtime)
    assert "重启中断" in restored.history("p")[0]["message"]


def test_requests_do_not_block_project_reads_and_detect_changes(runtime):
    entered, release = Event(), Event()

    def wait(messages):
        entered.set()
        assert release.wait(5)
        return call("respond", message="结果", citations=[])

    agent = Assistant(runtime, Scripted(wait))
    with ThreadPoolExecutor() as pool:
        future = pool.submit(agent.chat, "p", "hello", "slow")
        assert entered.wait(5)
        assert runtime.project_details("p")["name"] == "论文实验"
        with pytest.raises(ValueError, match="正在处理"):
            agent.chat("p", "again", "slow2")
        with pytest.raises(ValueError, match="正在处理"):
            agent.configure("https://api.deepseek.com", "deepseek-chat", "fake")
        runtime.add_task("p", "during request", "concurrent")
        release.set()
        assert future.result()["status"] == "error"


@pytest.mark.parametrize(
    "bad",
    [
        call("respond", message="made up", citations=["foreign"]),
        call("respond", message="", citations=[]),
        call("respond", message="bad fields", citations=[], extra=True),
        call("read_source", memory_id="foreign"),
        call("search_memory", query="x", project="other"),
        call("shell", command="whoami"),
        call("propose_changes", summary="oops", actions=[], extra=True),
        {
            "tool_calls": [
                {
                    "id": "x",
                    "function": {"name": "search_memory", "arguments": "bad-json"},
                }
            ]
        },
        {
            "tool_calls": [
                {"id": "x", "function": {"name": "search_memory", "arguments": "[]"}}
            ]
        },
    ],
)
def test_bad_model_tools_are_corrected_within_budget(runtime, bad):
    agent = Assistant(
        runtime, Scripted(bad, call("respond", message="已修正", citations=[]))
    )
    result = agent.chat("p", "hello", "bad")
    assert result["status"] == "completed"
    assert any(step["step"] == "tool_error" for step in result["trace"])


def test_loop_limit_and_non_tool_reply(runtime):
    agent = Assistant(
        runtime, Scripted(*[call("shell", command="no") for _ in range(6)])
    )
    assert "6 次" in agent.chat("p", "hi", "limit")["message"]
    agent.model = Scripted({"content": "fake success"})
    assert agent.chat("p", "hi", "plain")["status"] == "error"


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [{}],
        ["bad"],
        [{"kind": "delete"}],
        [{"kind": "add_task", "title": ""}],
        [{"kind": "add_task", "title": "x", "project": "other"}],
        [
            {
                "kind": "update_task",
                "task_id": "foreign",
                "title": "x",
                "completed": True,
                "revision": 1,
            }
        ],
        [{"kind": "save_record", "key": "k", "value": "v", "replaces": "foreign"}],
        [{"kind": "save_checkpoint", "note": ""}],
        [{"kind": "add_task", "title": "same"}] * 2,
    ],
)
def test_actions_validate_before_any_write(runtime, actions):
    with pytest.raises((ValueError, TypeError)):
        ProjectAdapter(runtime).validate_actions("p", actions)
    assert runtime.project_details("p")["tasks"] == []


def test_next_turn_includes_confirmed_conversation_and_latest_context(runtime):
    model = Scripted(
        propose({"kind": "add_task", "title": "下一步"}),
        call("respond", message="接着做", citations=[]),
    )
    agent = Assistant(runtime, model)
    result = agent.chat("p", "安排下一步", "first")
    agent.decide("p", result["id"], True)
    agent.chat("p", "继续", "second")
    context = model.messages[-1]
    assert context[1]["content"] == "安排下一步"
    assert "已执行并核验" in context[2]["content"]
    assert "下一步" in context[-1]["content"]


def test_agent_api_is_authenticated_and_configuration_never_returns_key(runtime):
    app = create_workbench(
        runtime,
        access_token="local-token",
        assistant_model=Scripted(call("respond", message="你好", citations=[])),
    )
    client = TestClient(app)
    assert client.get("/api/assistant/config").status_code == 401
    client.headers["Authorization"] = "Bearer local-token"
    response = client.post(
        "/api/assistant/config",
        json={
            "endpoint": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "secret-sentinel",
        },
    )
    assert response.status_code == 200
    assert "secret-sentinel" not in response.text
    assert "secret-sentinel" not in json.dumps(app.state.assistant.history("p"))
    app.state.assistant.model = Scripted(
        propose({"kind": "add_task", "title": "from API"})
    )
    result = client.post(
        "/api/assistant/chat",
        json={"project": "p", "message": "task", "request_id": "api"},
    ).json()
    assert result["status"] == "pending"
    assert (
        client.post(
            f"/api/assistant/runs/{result['id']}/decision",
            json={"project": "p", "approve": True},
        ).json()["status"]
        == "applied"
    )
    assert len(client.get("/api/assistant/history?project=p").json()["turns"]) == 1
    assert (
        client.post(
            "/api/assistant/chat",
            json={"project": "p", "message": "x", "request_id": "x"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.get("/assets/assistant.js").status_code == 200
    assert "项目助手" in client.get("/").text


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.example",
        "ftp://example.com",
        "https://user:pass@example.com",
        "https://example.com?key=x",
        "https://example.com#secret",
        "bad",
    ],
)
def test_model_endpoint_validation(endpoint):
    with pytest.raises(ValueError):
        CompatibleModel(endpoint, "model", "key")


def test_environment_config_is_independent_of_benchmark(runtime, monkeypatch):
    monkeypatch.setenv("DIVE_AGENT_API_KEY", "secret-env")
    agent = Assistant(runtime)
    assert agent.config()["model"] == "deepseek-chat"
    assert runtime.reader is None
    assert "secret-env" not in json.dumps(agent.config())


def test_engine_can_use_in_memory_ports_without_application():
    class Tools:
        def context(self, project):
            return {"goal": "test"}

    result = AgentEngine(
        Scripted(call("respond", message="ready", citations=[])), Tools()
    ).run("p", "hi", [])
    assert result["status"] == "completed"


def test_api_validation_does_not_echo_rejected_key(runtime):
    client = TestClient(create_workbench(runtime))
    secret = "never-echo-this" * 100
    response = client.post(
        "/api/assistant/config",
        json={
            "endpoint": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": secret,
        },
    )
    assert response.status_code == 422
    assert "never-echo-this" not in response.text
    assert client.post("/api/assistant/chat", json={"project": "p"}).status_code == 422


def test_record_and_projection_roll_back_with_failed_batch(runtime, monkeypatch):
    agent = Assistant(
        runtime,
        Scripted(
            propose(
                {
                    "kind": "save_record",
                    "key": "rollback fact",
                    "value": "must disappear",
                },
                {"kind": "save_checkpoint", "note": "fail"},
            )
        ),
    )
    result = agent.chat("p", "保存", "rollback-record")

    def fail(*args):
        raise ValueError("storage failure")

    monkeypatch.setattr(runtime, "checkpoint", fail)
    with pytest.raises(ValueError):
        agent.decide("p", result["id"], True)
    assert runtime.snapshot("p")["memories"] == []
    assert runtime.service.retrieve("p", "rollback fact").items == []


def test_backup_restore_keeps_pending_proposal_and_memory(tmp_path, runtime):
    agent = Assistant(
        runtime, Scripted(propose({"kind": "save_checkpoint", "note": "明天继续"}))
    )
    result = agent.chat("p", "留下交接", "backup")
    backup = tmp_path / "backup.db"
    runtime.backup(str(backup))
    restored_runtime = MemoryApplication(str(backup))
    try:
        restored = Assistant(restored_runtime)
        assert restored.history("p")[0]["status"] == "pending"
        assert restored.decide("p", result["id"], True)["status"] == "applied"
        assert (
            restored_runtime.project_details("p")["checkpoints"][0]["note"]
            == "明天继续"
        )
    finally:
        restored_runtime.close()


def test_remembering_same_fact_again_verifies_reinforcement(runtime):
    action = {"kind": "save_record", "key": "方法", "value": "使用 MAE"}
    agent = Assistant(runtime, Scripted(propose(action), propose(action)))
    for request_id in ("first", "second"):
        result = agent.chat("p", "再次记住相同的决定", request_id)
        assert agent.decide("p", result["id"], True)["status"] == "applied"
    assert runtime.lookup("p", "方法")["value"] == "使用 MAE"
    assert len(runtime.snapshot("p")["memories"]) == 1


def test_compact_context_still_supports_validated_task_edits(runtime):
    runtime.add_task("p", "核对结果", "seed")
    runtime.checkpoint("p", "准备核对", "", "checkpoint")

    def complete_known_task(messages):
        context = json.loads(messages[-1]["content"])["current_project_data"]
        assert context["task_summary"] == {"completed": 0, "open": 1}
        assert context["tasks_truncated"] is False
        task = context["tasks"][0]
        assert set(task) == {"id", "title", "completed", "revision"}
        assert context["checkpoints"][0]["note"] == "准备核对"
        return propose(
            {
                "kind": "update_task",
                "task_id": task["id"],
                "title": task["title"],
                "revision": task["revision"],
                "completed": True,
            }
        )

    agent = Assistant(runtime, Scripted(complete_known_task))
    result = agent.chat("p", "核对结果已经完成，请更新待办", "compact")
    assert result["status"] == "pending"
    assert agent.decide("p", result["id"], True)["status"] == "applied"
    assert ProjectAdapter(runtime).context("p")["task_summary"] == {
        "completed": 1,
        "open": 0,
    }


def test_context_reports_full_counts_when_task_details_are_truncated(runtime):
    for index in range(51):
        runtime.add_task("p", f"任务{index}", f"task-{index}")
    task = runtime.project_details("p")["tasks"][0]
    runtime.update_task("p", task["id"], task["title"], True, task["revision"])
    for index in range(4):
        runtime.checkpoint("p", f"交接{index}", "", f"point-{index}")
    context = ProjectAdapter(runtime).context("p")
    assert context["task_count"] == 51
    assert context["task_summary"] == {"completed": 1, "open": 50}
    assert context["tasks_truncated"] is True
    assert len(context["tasks"]) == 50
    assert len(context["checkpoints"]) == 3
    assert context["checkpoints"][0]["note"] == "交接3"


def test_additional_evidence_search_remains_available(runtime):
    runtime.record("p", ProjectRecord("评估方法", "MAE"), idempotency_key="method")
    memory_id = runtime.snapshot("p")["memories"][0]["id"]
    model = Scripted(
        call("search_memory", query="不存在的关键词"),
        call("search_memory", query="评估方法"),
        call("read_source", memory_id=memory_id),
        call("respond", message="使用 MAE。", citations=[memory_id]),
    )
    result = Assistant(runtime, model).chat("p", "评估方法是什么？", "evidence")
    assert result["status"] == "completed"
    assert result["citations"] == [memory_id]
    assert [step["step"] for step in result["trace"]] == [
        "context",
        "search_memory",
        "search_memory",
        "read_source",
    ]
