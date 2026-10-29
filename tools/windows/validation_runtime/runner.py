"""Run one authorized validation phase as a hidden, owned Windows task.

The frozen driver retains authorization, cost and semantic gates. This runner
adds an independently observed lifetime and a one-shot in-memory key handoff.
No exit code, including zero, establishes a benchmark score. Default: plan only.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .memory_pipe import MAX_PAYLOAD, MemoryPipe, process_birth, receive
else:
    from memory_pipe import MAX_PAYLOAD, MemoryPipe, process_birth, receive

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve()
CONTROL = SCRIPT.with_name("task_control.ps1")
KEY_ENV = "DIVE_READER_API_KEY"
SCHEMA = "dive-windows-validation-runtime-v1"
PHASES = ("challenge", "candidate", "full")
_SHA = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class RuntimeSpec:
    path: Path
    root: Path
    driver: Path
    driver_sha256: str
    proposal_sha256: str
    phase: str
    output: Path


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_path(value: str | Path, root: Path, *, existing: bool) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("A report path is required")
    path = Path(value)
    try:
        resolved = (path if path.is_absolute() else root / path).resolve(
            strict=existing
        )
        reports = (root / "eval" / "reports").resolve(strict=True)
    except OSError:
        raise ValueError("The saved report path is unavailable") from None
    if resolved == reports or not resolved.is_relative_to(reports):
        raise ValueError("Validation paths must remain inside repository eval/reports")
    return resolved


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate runtime specification field")
        result[key] = value
    return result


def load_spec(path: str | Path, *, root: Path = ROOT) -> RuntimeSpec:
    root = Path(root).resolve(strict=True)
    source = _report_path(path, root, existing=True)
    if not source.is_file() or source.stat().st_size > 65536:
        raise ValueError("A bounded saved runtime specification is required")
    obj = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    fields = {
        "schema_version",
        "driver",
        "driver_sha256",
        "proposal_sha256",
        "phase",
        "output",
    }
    if (
        not isinstance(obj, dict)
        or set(obj) != fields
        or obj["schema_version"] != SCHEMA
    ):
        raise ValueError("Runtime specification fields or version do not match")
    if obj["phase"] not in PHASES:
        raise ValueError("Unknown validation phase")
    for key in ("driver_sha256", "proposal_sha256"):
        if not isinstance(obj[key], str) or not _SHA.fullmatch(obj[key]):
            raise ValueError("A complete SHA256 binding is required")
    driver = _report_path(obj["driver"], root, existing=True)
    if not driver.is_file() or driver.suffix != ".py":
        raise ValueError("The frozen validation driver must be a Python file")
    output = _report_path(obj["output"], root, existing=False)
    if (
        output == source
        or output == driver
        or source.is_relative_to(output)
        or driver.is_relative_to(output)
    ):
        raise ValueError("Runtime output must not own its specification or driver")
    return RuntimeSpec(
        source,
        root,
        driver,
        obj["driver_sha256"],
        obj["proposal_sha256"],
        obj["phase"],
        output,
    )


def _current(spec: RuntimeSpec) -> None:
    if load_spec(spec.path, root=spec.root) != spec:
        raise ValueError("Runtime specification changed")
    if digest(spec.driver) != spec.driver_sha256:
        raise ValueError("Frozen driver bytes changed")


def load_driver(spec: RuntimeSpec) -> Any:
    _current(spec)
    source = spec.driver.read_bytes()
    if hashlib.sha256(source).hexdigest() != spec.driver_sha256:
        raise ValueError("Frozen driver bytes changed before compilation")
    description = importlib.util.spec_from_file_location(
        "dive_frozen_validation_" + spec.driver_sha256, spec.driver
    )
    if description is None or description.loader is None:
        raise ValueError("Validation driver cannot be loaded")
    module = importlib.util.module_from_spec(description)
    # Compile the exact bytes just hashed; timestamp/size based bytecode caches
    # cannot establish that the executed code matches a frozen source hash.
    sys.modules[description.name] = module
    try:
        exec(compile(source, str(spec.driver), "exec"), module.__dict__)
    except BaseException:
        if sys.modules.get(description.name) is module:
            del sys.modules[description.name]
        raise
    return module


def prepare(spec: RuntimeSpec, *, execute: bool = False) -> dict[str, Any]:
    """Delegate authorization before any prompt, output creation or OS mutation."""
    driver = load_driver(spec)
    context = driver.prepare(spec.phase, execute=execute)
    if not isinstance(context, dict) or not isinstance(context.get("plan"), dict):
        raise ValueError("Frozen driver did not return a validation plan")
    plan = context["plan"]
    if plan.get("proposal_sha256") != spec.proposal_sha256:
        raise ValueError("Frozen proposal binding changed")
    if plan.get("driver_sha256", spec.driver_sha256) != spec.driver_sha256:
        raise ValueError("Driver plan binding changed")
    if plan.get("phase", spec.phase) != spec.phase:
        raise ValueError("Driver phase binding changed")
    if execute and plan.get("execution_blockers"):
        raise ValueError("Frozen driver still reports execution blockers")
    _current(spec)
    return context


def _write(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
    else:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected an evidence object")
    return value


def task_control(spec: RuntimeSpec, mode: str, **values: str | int) -> dict[str, Any]:
    if os.name != "nt":
        raise ValueError("Windows task control is required")
    arguments = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(CONTROL),
        "-Mode",
        mode,
        "-Output",
        str(spec.output),
    ]
    for name, value in values.items():
        arguments.extend(("-" + name, str(value)))
    result = subprocess.run(
        arguments,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Task control failed; inspect retained evidence without restarting"
        )
    return json.loads(result.stdout)


def _worker_identity(spec: RuntimeSpec) -> tuple[int, int]:
    worker = _read(spec.output / "worker.json")
    identity = task_control(spec, "VerifyWorker")
    if identity.get("verified") is not True or identity.get("pid") != worker.get("pid"):
        raise ValueError("The OS task worker identity was not established")
    pid = identity["pid"]
    birth = process_birth(pid)
    if type(pid) is not int or birth != worker.get("process_birth"):
        raise ValueError("Worker creation identity changed")
    _write(spec.output / "verified-worker.json", identity, exclusive=True)
    return pid, birth


def launch(
    spec: RuntimeSpec, *, key_supplier: Callable[[str], str] = getpass.getpass
) -> dict[str, Any]:
    prepare(spec, execute=True)
    if spec.output.exists():
        raise ValueError("Runtime output exists; no restart or overwrite")
    if os.name != "nt":
        raise ValueError("Windows task launch is required")
    # Do not read a saved environment key. Reading the credential is the last
    # input step, after the frozen authorization and exclusive-output checks.
    key = key_supplier("API credential (hidden, memory only): ")
    payload = bytearray()
    pipe = None
    directory_owned = False
    record = {
        "schema_version": SCHEMA,
        "status": "LAUNCHING",
        "phase": spec.phase,
        "started_at_utc": utc(),
        "launcher_pid": os.getpid(),
        "spec_sha256": digest(spec.path),
        "driver_sha256": spec.driver_sha256,
        "proposal_sha256": spec.proposal_sha256,
        "runtime_sources_sha256": {
            p.name: digest(p)
            for p in (SCRIPT, CONTROL, SCRIPT.with_name("memory_pipe.py"))
        },
        "automatic_restart": False,
        "credential_channel": "one-shot current-user local named pipe",
        "semantic_result": "NOT_ESTABLISHED_BY_LAUNCH",
    }
    try:
        if (
            not isinstance(key, str)
            or not key
            or len(key.encode("utf-8")) > MAX_PAYLOAD
        ):
            raise ValueError("A bounded in-memory credential is required")
        payload = bytearray(key.encode("utf-8"))
        key = ""
        prepare(spec, execute=True)
        spec.output.mkdir(parents=True, exist_ok=False)
        directory_owned = True
        _write(spec.output / "launch.json", record, exclusive=True)
        pipe = MemoryPipe()
        interpreter = Path(sys.executable).with_name("pythonw.exe").resolve(strict=True)
        actual_interpreter = (
            Path(getattr(sys, "_base_executable", sys.executable))
            .with_name("pythonw.exe")
            .resolve(strict=True)
        )
        control = task_control(
            spec,
            "Launch",
            PipeName=pipe.name,
            ServerPID=os.getpid(),
            ServerBirth=process_birth(os.getpid()),
            Interpreter=str(interpreter),
            ExpectedWorkerExecutable=str(actual_interpreter),
            Runner=str(SCRIPT),
            Spec=str(spec.path),
            Repository=str(spec.root),
        )
        if (
            control.get("status") != "START_REQUESTED"
            or control.get("created_by_this_runtime") is not True
        ):
            raise RuntimeError("Task start not established; no automatic retry")
        deadline = time.monotonic() + 20
        while not (spec.output / "worker.json").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Worker observation expired; inspect the same task without restarting"
                )
            time.sleep(0.1)
        pid, birth = _worker_identity(spec)
        pipe.deliver(
            payload, expected_pid=pid, expected_birth=birth, timeout_seconds=20
        )
        record.update(
            status="HANDED_OFF",
            worker_pid=pid,
            worker_birth=birth,
            launcher_returned_at_utc=utc(),
        )
    except BaseException as exc:  # noqa: BLE001 - credential-bearing exceptions are never printed
        record.update(
            status="FAILED_WITHOUT_RETRY",
            exception_type=type(exc).__name__,
            finished_at_utc=utc(),
        )
        if directory_owned:
            _write(spec.output / "launch.json", record)
        raise RuntimeError(
            "Validation launch failed; inspect exception type and exact owned task"
        ) from None
    finally:
        key = ""
        payload[:] = b"\x00" * len(payload)
        if pipe is not None:
            pipe.close()
    _write(spec.output / "launch.json", record)
    return record


class _RedactedLog:
    """Stream text without persisting a credential split across write calls."""

    def __init__(self, stream: Any, secret: str):
        self.stream, self.secret, self.pending = stream, secret, ""

    def write(self, text: str) -> int:
        self.pending += str(text)
        keep = len(self.secret) - 1
        # Replace a whole key before separating the retained suffix, so a match
        # crossing that boundary is never emitted in pieces.
        self.pending = self.pending.replace(self.secret, "[credential redacted]")
        emit = max(0, len(self.pending) - keep)
        if emit:
            self.stream.write(self.pending[:emit])
            self.pending = self.pending[emit:]
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def finish(self) -> None:
        self.stream.write(self.pending.replace(self.secret, "[credential redacted]"))
        self.pending = ""
        self.stream.flush()


def worker(
    spec: RuntimeSpec,
    pipe_name: str,
    server_pid: int,
    server_birth: int,
    *,
    heartbeat_seconds: float = 5,
) -> int:
    """Revalidate the driver before receiving the key, then run exactly one phase."""
    if not 0 < heartbeat_seconds <= 60:
        raise ValueError("A bounded heartbeat interval is required")
    os.environ.pop(KEY_ENV, None)
    _current(spec)
    launch_record = _read(spec.output / "launch.json")
    if (
        launch_record.get("spec_sha256") != digest(spec.path)
        or launch_record.get("driver_sha256") != spec.driver_sha256
        or launch_record.get("proposal_sha256") != spec.proposal_sha256
        or launch_record.get("phase") != spec.phase
        or launch_record.get("runtime_sources_sha256")
        != {
            p.name: digest(p)
            for p in (SCRIPT, CONTROL, SCRIPT.with_name("memory_pipe.py"))
        }
    ):
        raise ValueError("Worker launch binding changed")
    claim = {"phase": spec.phase, "pid": os.getpid(), "claimed_at_utc": utc()}
    _write(spec.output / "worker-claim.json", claim, exclusive=True)
    state = {
        "schema_version": SCHEMA,
        "phase": spec.phase,
        "pid": os.getpid(),
        "process_birth": process_birth(os.getpid()),
        "started_at_utc": utc(),
        "status": "PREPARING",
        "exit_code": None,
        "semantic_result": "NOT_ESTABLISHED_BY_PROCESS_EXIT",
    }
    _write(spec.output / "worker.json", state, exclusive=True)
    payload = bytearray()
    key = ""
    stop = threading.Event()
    started = time.monotonic()
    thread = None
    code = 2
    try:
        prepare(spec, execute=True)
        state["status"] = "WAITING_FOR_MEMORY_HANDOFF"
        _write(spec.output / "worker.json", state)
        payload = receive(
            pipe_name,
            expected_server_pid=server_pid,
            expected_server_birth=server_birth,
            timeout_seconds=20,
        )
        key = payload.decode("utf-8")
        if not key:
            raise ValueError("Empty worker credential")
        state["status"] = "RUNNING"
        _write(spec.output / "worker.json", state)

        def heartbeat() -> None:
            index = 0
            while not stop.wait(heartbeat_seconds):
                index += 1
                _write(
                    spec.output / "heartbeat.json",
                    {
                        "phase": spec.phase,
                        "pid": os.getpid(),
                        "index": index,
                        "at_utc": utc(),
                        "elapsed_seconds": time.monotonic() - started,
                    },
                )

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        driver = load_driver(spec)
        os.environ[KEY_ENV] = key
        with (
            (spec.output / "stdout.txt").open("x", encoding="utf-8") as stdout,
            (spec.output / "stderr.txt").open("x", encoding="utf-8") as stderr,
        ):
            out, err = _RedactedLog(stdout, key), _RedactedLog(stderr, key)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = driver.main(["--phase", spec.phase, "--execute"])
            finally:
                out.finish()
                err.finish()
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError("Driver returned no bounded process exit code")
        state["status"] = "COMPLETED" if code == 0 else "FAILED"
    except BaseException as exc:  # noqa: BLE001 - persist type, never secret-bearing exception text
        state.update(status="FAILED", exception_type=type(exc).__name__)
        code = 2
    finally:
        os.environ.pop(KEY_ENV, None)
        key = ""
        payload[:] = b"\x00" * len(payload)
        stop.set()
        if thread is not None:
            thread.join(timeout=10)
        state.update(
            exit_code=code,
            finished_at_utc=utc(),
            elapsed_seconds=time.monotonic() - started,
        )
        _write(spec.output / "terminal.json", state, exclusive=True)
        _write(spec.output / "worker.json", state)
    return code


def observe(spec: RuntimeSpec) -> dict[str, Any]:
    return task_control(spec, "Observe")


def cleanup(spec: RuntimeSpec) -> dict[str, Any]:
    return task_control(spec, "Cleanup")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--execute", action="store_true")
    actions.add_argument("--observe", action="store_true")
    actions.add_argument("--cleanup", action="store_true")
    actions.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pipe", help=argparse.SUPPRESS)
    parser.add_argument("--server-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--server-birth", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
        if args.worker:
            if not args.pipe or not args.server_pid or not args.server_birth:
                raise ValueError("Worker requires one exact memory handoff identity")
            return worker(spec, args.pipe, args.server_pid, args.server_birth)
        if args.execute:
            result = launch(spec)
        elif args.observe:
            result = observe(spec)
        elif args.cleanup:
            result = cleanup(spec)
        else:
            result = prepare(spec, execute=False)["plan"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BaseException as exc:  # noqa: BLE001 - never surface credentials in exception messages
        print(
            json.dumps(
                {
                    "status": "STOPPED_WITHOUT_RETRY",
                    "exception_type": type(exc).__name__,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
