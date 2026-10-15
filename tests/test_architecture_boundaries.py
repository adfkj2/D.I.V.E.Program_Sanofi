"""Executable dependency direction for the reusable memory and application layers."""

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src/dive_memory"


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
