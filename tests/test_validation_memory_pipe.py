"""Exercise real local Windows processes; never contact a provider."""

import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows pipe")
SOURCE = (
    Path(__file__).resolve().parents[1]
    / "tools/windows/validation_runtime/memory_pipe.py"
)


@pytest.fixture
def pipe_module():
    spec = importlib.util.spec_from_file_location("validation_pipe_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHILD = """
import hashlib, importlib.util, json, sys
spec = importlib.util.spec_from_file_location('local_pipe_child', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    marker = module.receive(sys.argv[2], expected_server_pid=int(sys.argv[3]),
                            expected_server_birth=int(sys.argv[4]), timeout_seconds=2)
except module.HandoffRejected:
    print(json.dumps({'rejected': True}))
    raise SystemExit(3)
print(json.dumps({'digest': hashlib.sha256(marker).hexdigest()}))
marker[:] = b'\\x00' * len(marker)
"""


@pytest.mark.parametrize(
    "mismatch", [None, "client-pid", "client-birth", "server-pid", "server-birth"]
)
def test_handoff_requires_exact_live_peer_identity(pipe_module, mismatch):
    server = pipe_module.MemoryPipe()
    marker = ("synthetic-r69:" + secrets.token_hex(48)).encode()
    expected = hashlib.sha256(marker).hexdigest()
    server_pid = os.getpid() + (1 if mismatch == "server-pid" else 0)
    server_birth = pipe_module.process_birth(os.getpid()) + (
        1 if mismatch == "server-birth" else 0
    )
    command = [
        sys._base_executable,
        "-c",
        CHILD,
        str(SOURCE),
        server.name,
        str(server_pid),
        str(server_birth),
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET"))
    }
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rejected = False
    try:
        birth = pipe_module.process_birth(process.pid)
        server.deliver(
            marker,
            expected_pid=os.getpid() if mismatch == "client-pid" else process.pid,
            expected_birth=birth + (1 if mismatch == "client-birth" else 0),
            timeout_seconds=3,
        )
    except pipe_module.HandoffRejected:
        rejected = True
    finally:
        server.close()
    stdout, stderr = process.communicate(timeout=5)
    output = json.loads(stdout)
    assert not stderr
    assert marker not in stdout + stderr
    assert marker.decode() not in " ".join(command)
    assert server.handle is None
    if mismatch:
        assert rejected and process.returncode == 3 and output == {"rejected": True}
    else:
        assert (
            not rejected and process.returncode == 0 and output == {"digest": expected}
        )
    with pytest.raises(pipe_module.HandoffRejected):
        pipe_module.receive(
            server.name,
            expected_server_pid=os.getpid(),
            expected_server_birth=pipe_module.process_birth(os.getpid()),
            timeout_seconds=0.1,
        )


def test_no_client_expires_without_leaving_an_endpoint(pipe_module):
    server = pipe_module.MemoryPipe()
    started = time.monotonic()
    with pytest.raises(pipe_module.HandoffRejected):
        server.deliver(
            b"synthetic",
            expected_pid=os.getpid(),
            expected_birth=pipe_module.process_birth(os.getpid()),
            timeout_seconds=0.05,
        )
    assert time.monotonic() - started < 2
    assert server.handle is None


@pytest.mark.parametrize("payload", [b"", b"x" * 4097])
def test_invalid_payload_closes_one_shot_endpoint(pipe_module, payload):
    server = pipe_module.MemoryPipe()
    with pytest.raises(pipe_module.HandoffRejected):
        server.deliver(
            payload,
            expected_pid=os.getpid(),
            expected_birth=pipe_module.process_birth(os.getpid()),
        )
    assert server.handle is None


@pytest.mark.parametrize(
    "endpoint",
    [
        r"\\remote\pipe\DIVE-R69-" + "a" * 48,
        r"\\.\pipe\unrelated",
        r"\\.\pipe\DIVE-R69-" + "g" * 48,
    ],
)
def test_other_or_remote_endpoints_are_rejected_before_connection(
    pipe_module, endpoint
):
    with pytest.raises(pipe_module.HandoffRejected):
        pipe_module.receive(
            endpoint, expected_server_pid=os.getpid(), expected_server_birth=1
        )
