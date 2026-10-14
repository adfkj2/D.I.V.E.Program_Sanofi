"""Launch real child servers without requiring an interactive desktop in CI."""

import importlib.util
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "windows_launcher", ROOT / "tools/windows/launcher.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def controller(tmp_path):
    app = launcher.Launcher(
        ROOT,
        data_dir=tmp_path / "状态 空格",
        db=tmp_path / "记录 空格.db",
        preferred_port=0,
    )
    yield app
    app.stop()


def request(state, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(
        launcher.local_url(state) + path,
        data=json.dumps(body).encode() if body else None,
        headers=headers,
    )
    with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
        return json.load(response)


def test_only_one_launcher_owns_a_project_and_lock_recovers(tmp_path):
    first = launcher.InstanceLock(tmp_path / "launcher.lock")
    second = launcher.InstanceLock(tmp_path / "launcher.lock")
    try:
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"port": True},
        {"port": 70000},
        {"port": 80, "instance": "x" * 32},
        {"url": "https://example.org"},
    ],
)
def test_invalid_state_cannot_open_an_arbitrary_address(tmp_path, payload):
    path = tmp_path / "running.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert launcher.read_state(path) is None
    assert launcher.running_instance(path) is None


def test_state_cleanup_never_removes_a_newer_instance(tmp_path):
    path = tmp_path / "running.json"
    payload = {"port": 12345, "instance": "a" * 32}
    path.write_text(json.dumps(payload), encoding="utf-8")
    launcher.remove_state(path, "b" * 32)
    assert launcher.read_state(path) == payload


def test_other_http_service_is_not_accepted_as_this_workbench(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"alive"}')

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = tmp_path / "running.json"
        path.write_text(
            json.dumps({"port": server.server_port, "instance": "a" * 32}),
            encoding="utf-8",
        )
        assert launcher.running_instance(path) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_start_repeat_detection_stop_restart_and_persistence(
    controller, monkeypatch
):
    monkeypatch.setenv("DIVE_MODE", "live")
    monkeypatch.setenv("DIVE_WORKBENCH_TOKEN", "local-test-token")
    state = controller.start()
    process = controller.process
    assert controller.start() == state
    assert controller.process is process
    assert launcher.running_instance(controller.state_file) == state
    assert launcher.running_instance(controller.state_file, "b" * 32) is None
    # Preserve an explicitly configured local access boundary, while forcing the
    # shortcut's documented offline mode even if the shell enabled live mode.
    with pytest.raises(HTTPError) as failure:
        request(state, "api/readiness")
    assert failure.value.code == 401
    assert (
        request(state, "api/readiness", token="local-test-token")["mode"]
        == "evidence_only"
    )
    request(
        state,
        "api/records",
        {
            "project": "launcher-test",
            "key": "budget",
            "value": "20",
            "idempotency_key": "first",
            "observed_at": "2026-01-01",
        },
        token="local-test-token",
    )
    controller.stop()
    assert controller.process.poll() == 0
    assert not controller.state_file.exists()
    controller.cancel.clear()
    restarted = controller.start()
    assert restarted["instance"] != state["instance"]
    answer = request(
        restarted,
        "api/consult",
        {"project": "launcher-test", "question": "budget"},
        token="local-test-token",
    )
    assert answer["status"] == "ANSWER" and "20" in answer["answer"]
    assert answer["model_calls"] == 0


def test_busy_port_uses_reserved_fallback_without_touching_other_service(controller):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        controller.preferred_port = occupied.getsockname()[1]
        state = controller.start()
        assert state["port"] != controller.preferred_port
        controller.stop()
        assert occupied.getsockname()[1] == controller.preferred_port
        assert occupied.fileno() >= 0


def test_cancel_before_start_does_not_create_child(controller):
    controller.cancel.set()
    with pytest.raises(launcher.LaunchError, match="取消"):
        controller.start()
    assert controller.process is None


def test_dependency_install_failure_is_reported_and_separate_from_existing_env(
    controller, monkeypatch
):
    monkeypatch.setattr(launcher, "runtime_available", lambda *_: False)
    attempted = []

    def fail(command):
        attempted.append(command)
        raise launcher.LaunchError("运行环境准备失败")

    monkeypatch.setattr(controller, "command", fail)
    with pytest.raises(launcher.LaunchError, match="准备失败"):
        controller.prepare_runtime()
    assert attempted[0][-1] == str(ROOT / ".venv-workbench")
    assert controller.process is None


def test_child_startup_failure_has_a_log_and_is_not_reported_ready(
    controller, tmp_path
):
    controller.db = tmp_path  # SQLite cannot open a directory as a database.
    with pytest.raises(launcher.LaunchError, match="未能启动"):
        controller.start()
    assert controller.process.poll() is not None
    assert controller.log_file.stat().st_size > 0
    assert launcher.running_instance(controller.state_file) is None
