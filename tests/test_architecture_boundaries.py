"""Executable dependency direction for the reusable memory and application layers."""

import ast
import subprocess
import sys
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src/dive_memory"


def test_product_and_shared_reading_do_not_depend_on_evaluation():
    paths = [
        *(ROOT / "reading").glob("*.py"),
        *(p for p in (ROOT / "application").glob("*.py") if p.name != "oracle_run.py"),
    ]
    for path in paths:
        for module in imports(path):
            assert not module.startswith(
                ("dive_memory.oracle", "dive_memory.eval_adapters")
            ), (path, module)


def test_product_can_run_when_benchmark_imports_are_unavailable():
    # A fresh process catches transitive imports that an AST-only check misses.
    code = """
import importlib.abc
import sys
class NoBenchmark(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "dive_memory.oracle" or fullname.startswith("dive_memory.oracle."):
            raise ImportError("Benchmark modules unavailable")
sys.meta_path.insert(0, NoBenchmark())
from dive_memory.application.readers import create_reader
from dive_memory.application.runtime import MemoryApplication
from dive_memory.application.records import ProjectRecord
from dive_memory.reading.providers import ScriptedChatClient
reader = create_reader(ScriptedChatClient(['{"status":"ABSTAIN","answer":null,"citations":[],"missing":["fixture"]}']), model="fixture")
app = MemoryApplication(reader=reader)
try:
    app.record("project", ProjectRecord("budget", "20"), idempotency_key="one")
    assert app.lookup("project", "budget")["value"] == "20"
    assert app.answer("project", "budget")["status"] == "ABSTAIN"
finally:
    app.close()
assert not any(k == "dive_memory.oracle" or k.startswith("dive_memory.oracle.") for k in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def imports(path):
    relative = path.relative_to(ROOT).with_suffix("")
    package = ".".join(("dive_memory", *relative.parts[:-1]))
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            yield module
            yield from (module + "." + alias.name for alias in node.names)


def test_production_modules_do_not_import_experiment_directories():
    for path in ROOT.rglob("*.py"):
        for module in imports(path):
            assert module.split(".")[0] not in {
                "ops",
                "audit_tools",
                "eval",
                "tests",
            }, (path, module)


def test_memory_core_cannot_depend_on_delivery_or_http_frameworks():
    for filename in (
        "service.py",
        "store.py",
        "models.py",
        "temporal.py",
        "retrieval.py",
        "context.py",
        "observability.py",
    ):
        path = ROOT / filename
        for module in imports(path):
            assert not any(
                module == banned or module.startswith(banned + ".")
                for banned in (
                    "dive_memory.application",
                    "dive_memory.web",
                    "dive_memory.cli",
                    "dive_memory.api",
                    "fastapi",
                    "starlette",
                )
            ), (path, module)


def test_application_does_not_import_cli_or_http_adapters():
    for path in (ROOT / "application").glob("*.py"):
        for module in imports(path):
            assert not any(
                module == banned or module.startswith(banned + ".")
                for banned in (
                    "dive_memory.web",
                    "dive_memory.cli",
                    "dive_memory.api",
                    "fastapi",
                    "starlette",
                )
            ), (path, module)


def test_agent_orchestration_and_benchmark_are_independent():
    for path in (ROOT / "agent").glob("*.py"):
        for module in imports(path):
            assert not any(
                module == banned or module.startswith(banned + ".")
                for banned in (
                    "dive_memory.application",
                    "dive_memory.oracle",
                    "dive_memory.web",
                    "dive_memory.store",
                    "fastapi",
                    "starlette",
                )
            ), (path, module)
    for path in (ROOT / "oracle").glob("*.py"):
        for module in imports(path):
            assert not module.startswith(
                (
                    "dive_memory.agent",
                    "dive_memory.application.assistant",
                    "dive_memory.application.agent_tools",
                )
            ), (path, module)
