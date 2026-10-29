"""A new Windows execution path must preserve frozen-driver authorization."""

import hashlib
import importlib.util
import json
import os
import py_compile
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).parents[1] / "tools/windows/validation_runtime/runner.py"


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE.parent))
    spec = importlib.util.spec_from_file_location("validation_runtime_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def frozen_spec(tmp_path):
    directory = tmp_path / "eval/reports/synthetic"
    directory.mkdir(parents=True)
    driver = directory / "driver.py"
    driver.write_text("# Offline frozen driver fixture\n", encoding="utf-8")
    payload = {
        "schema_version": "dive-windows-validation-runtime-v1",
        "driver": "eval/reports/synthetic/driver.py",
        "driver_sha256": hashlib.sha256(driver.read_bytes()).hexdigest(),
        "proposal_sha256": "a" * 64,
        "phase": "challenge",
        "output": "eval/reports/synthetic/runtime-unique",
    }
    path = directory / "runtime-spec.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path, path, payload


def reject_type(runtime):
    # Public helpers report invalid specifications as ValueError-compatible
    # failures. Native OS and provider exceptions are not accepted as proof.
    return ValueError


def test_default_plan_is_read_only_and_does_not_read_key(
    runtime, frozen_spec, monkeypatch
):
    root, path, _ = frozen_spec
    spec = runtime.load_spec(path, root=root)
    calls = []

    def prepare(phase, *, execute=False):
        calls.append((phase, execute))
        return {"plan": {"proposal_sha256": "a" * 64, "blockers": []}}

    monkeypatch.setattr(
        runtime, "load_driver", lambda spec: SimpleNamespace(prepare=prepare)
    )
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    runtime.prepare(spec)
    assert calls == [("challenge", False)]
    assert not spec.output.exists()
    assert before == {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "another"),
        ("phase", "retry"),
        ("phase", True),
        ("driver", "../outside.py"),
        ("driver", "src/driver.py"),
        ("output", "../outside"),
        ("output", "eval/reports"),
        ("driver_sha256", "x" * 64),
        ("proposal_sha256", "a" * 63),
    ],
)
def test_invalid_spec_rejected_before_loading_driver(
    runtime, frozen_spec, field, value
):
    root, path, payload = frozen_spec
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(reject_type(runtime)):
        runtime.load_spec(path, root=root)


def test_duplicate_spec_key_is_rejected(runtime, frozen_spec):
    root, path, payload = frozen_spec
    raw = json.dumps(payload)
    path.write_text(raw[:-1] + ', "phase":"full"}', encoding="utf-8")
    with pytest.raises(reject_type(runtime)):
        runtime.load_spec(path, root=root)


def test_changed_driver_cannot_be_imported(runtime, frozen_spec, monkeypatch):
    root, path, _ = frozen_spec
    spec = runtime.load_spec(path, root=root)
    imported = root / "changed-source-was-imported.txt"
    spec.driver.write_text(
        "from pathlib import Path\n"
        + f"Path({str(imported)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    with pytest.raises(reject_type(runtime)):
        runtime.prepare(spec, execute=True)
    assert not imported.exists() and not spec.output.exists()


def test_driver_executes_bound_source_when_timestamp_cache_is_stale(
    runtime, frozen_spec
):
    root, path, payload = frozen_spec
    driver = root / payload["driver"]
    driver.write_text("MARKER = 'A'\n", encoding="utf-8")
    py_compile.compile(str(driver), doraise=True)
    old = driver.stat()
    driver.write_text("MARKER = 'B'\n", encoding="utf-8")
    os.utime(driver, ns=(old.st_atime_ns, old.st_mtime_ns))
    payload["driver_sha256"] = hashlib.sha256(driver.read_bytes()).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = runtime.load_driver(runtime.load_spec(path, root=root))
    assert loaded.MARKER == "B"


@pytest.mark.parametrize(
    "plan",
    [
        {"proposal_sha256": "b" * 64, "execution_blockers": []},
        {
            "proposal_sha256": "a" * 64,
            "driver_sha256": "b" * 64,
            "execution_blockers": [],
        },
        {"proposal_sha256": "a" * 64, "execution_blockers": ["old grant stopped"]},
    ],
)
def test_unapproved_plan_is_rejected_before_key_or_task(
    runtime, frozen_spec, monkeypatch, plan
):
    root, path, _ = frozen_spec
    spec = runtime.load_spec(path, root=root)
    calls = []
    monkeypatch.setattr(
        runtime,
        "load_driver",
        lambda spec: SimpleNamespace(prepare=lambda *args, **kwargs: {"plan": plan}),
    )
    with pytest.raises(reject_type(runtime)):
        runtime.launch(spec, key_supplier=lambda *args: calls.append("key-read"))
    assert not calls and not spec.output.exists()


def test_existing_output_prevents_new_key_read_or_execution(
    runtime, frozen_spec, monkeypatch
):
    root, path, _ = frozen_spec
    spec = runtime.load_spec(path, root=root)
    spec.output.mkdir()
    marker = spec.output / "old-pending.json"
    marker.write_text('{"state":"pending"}\n', encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "load_driver",
        lambda spec: SimpleNamespace(
            prepare=lambda *args, **kwargs: {
                "plan": {"proposal_sha256": "a" * 64, "blockers": []}
            }
        ),
    )
    reads = []
    with pytest.raises(reject_type(runtime)):
        runtime.launch(spec, key_supplier=lambda *args: reads.append("read"))
    assert reads == [] and marker.read_text() == '{"state":"pending"}\n'


def test_driver_owns_authorization_for_the_exact_phase(
    runtime, frozen_spec, monkeypatch
):
    root, path, _ = frozen_spec
    spec = runtime.load_spec(path, root=root)
    calls = []

    def prepare(phase, *, execute=False):
        calls.append((phase, execute))
        if execute:
            raise ValueError("No new authorization after a stopped attempt")
        return {"plan": {"proposal_sha256": "a" * 64, "blockers": ["not authorized"]}}

    monkeypatch.setattr(
        runtime, "load_driver", lambda spec: SimpleNamespace(prepare=prepare)
    )
    runtime.prepare(spec)
    with pytest.raises(ValueError):
        runtime.launch(
            spec, key_supplier=lambda *args: pytest.fail("Must not ask for a key")
        )
    assert calls == [("challenge", False), ("challenge", True)]
    assert not spec.output.exists()


@pytest.mark.parametrize(
    "chunks",
    [
        ["prefix syn", "thetic-marker suffix"],
        list("synthetic-marker"),
        ["synthetic-marker" * 3],
        ["abc synthetic-mark", "er", " synthetic-marker end"],
    ],
)
def test_log_redacts_complete_credentials_across_write_boundaries(runtime, chunks):
    output = StringIO()
    log = runtime._RedactedLog(output, "synthetic-marker")
    for chunk in chunks:
        assert log.write(chunk) == len(chunk)
        log.flush()
        assert "synthetic-marker" not in output.getvalue()
    log.finish()
    assert "synthetic-marker" not in output.getvalue()
    assert "[credential redacted]" in output.getvalue()


def worker_setup(
    runtime, frozen_spec, monkeypatch, *, result=0, error=False, blocked=False
):
    root, path, _ = frozen_spec
    spec = runtime.load_spec(path, root=root)
    spec.output.mkdir()
    sources = (
        runtime.SCRIPT,
        runtime.CONTROL,
        runtime.SCRIPT.with_name("memory_pipe.py"),
    )
    launch = {
        "phase": spec.phase,
        "spec_sha256": runtime.digest(path),
        "driver_sha256": spec.driver_sha256,
        "proposal_sha256": spec.proposal_sha256,
        "runtime_sources_sha256": {p.name: runtime.digest(p) for p in sources},
    }
    (spec.output / "launch.json").write_text(json.dumps(launch), encoding="utf-8")
    payload = bytearray(b"synthetic-marker")
    calls = []

    def prepare(spec, *, execute=False):
        calls.append(("prepare", execute))
        if blocked:
            raise ValueError("Prior execution has stopped")
        return {"plan": {"proposal_sha256": spec.proposal_sha256}}

    def receive(*args, **kwargs):
        calls.append(("receive", True))
        return payload

    def main(argv):
        calls.append(("main", tuple(argv)))
        assert runtime.os.environ[runtime.KEY_ENV] == "synthetic-marker"
        print("prefix synthetic-", end="")
        print("marker suffix")
        if error:
            raise RuntimeError(
                "Provider exception with synthetic-marker must remain private"
            )
        return result

    monkeypatch.setattr(runtime, "process_birth", lambda pid: 123)
    monkeypatch.setattr(runtime, "prepare", prepare)
    monkeypatch.setattr(runtime, "receive", receive)
    monkeypatch.setattr(runtime, "load_driver", lambda spec: SimpleNamespace(main=main))
    monkeypatch.setenv(runtime.KEY_ENV, "inherited-key-must-be-cleared")
    return spec, payload, calls


@pytest.mark.parametrize(
    "result,error,expected",
    [(0, False, 0), (3, False, 3), (False, False, 2), (0, True, 2)],
)
def test_worker_records_exit_without_claiming_score_or_persisting_key(
    runtime, frozen_spec, monkeypatch, result, error, expected
):
    spec, payload, calls = worker_setup(
        runtime, frozen_spec, monkeypatch, result=result, error=error
    )
    assert (
        runtime.worker(spec, "synthetic-endpoint", 42, 123, heartbeat_seconds=0.01)
        == expected
    )
    terminal = json.loads((spec.output / "terminal.json").read_text(encoding="utf-8"))
    assert terminal["exit_code"] == expected
    assert terminal["status"] == ("COMPLETED" if expected == 0 else "FAILED")
    assert terminal["semantic_result"] == "NOT_ESTABLISHED_BY_PROCESS_EXIT"
    assert runtime.KEY_ENV not in runtime.os.environ
    assert payload == bytearray(len(payload))
    for path in spec.output.rglob("*"):
        if path.is_file():
            assert b"synthetic-marker" not in path.read_bytes()
    before = (spec.output / "terminal.json").read_bytes()
    with pytest.raises(FileExistsError):
        runtime.worker(spec, "synthetic-endpoint", 42, 123)
    assert (spec.output / "terminal.json").read_bytes() == before
    assert sum(c[0] == "main" for c in calls) == 1


def test_worker_rechecks_authorization_before_receiving_key(
    runtime, frozen_spec, monkeypatch
):
    spec, _, calls = worker_setup(runtime, frozen_spec, monkeypatch, blocked=True)
    assert runtime.worker(spec, "synthetic-endpoint", 42, 123) == 2
    assert calls == [("prepare", True)]
    assert runtime.KEY_ENV not in runtime.os.environ
    assert json.loads((spec.output / "terminal.json").read_text())["status"] == "FAILED"
