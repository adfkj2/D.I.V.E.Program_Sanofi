from __future__ import annotations

import importlib.metadata
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


def sha256_file(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def code_snapshot(repository_root: Path) -> dict[str, Any]:
    """Fingerprint actual Python sources, including uncommitted/new modules."""
    root = repository_root.resolve()
    paths = [p for folder in (root / "src", root / "ops") for p in folder.rglob("*.py")]
    if (root / "pyproject.toml").is_file():
        paths.append(root / "pyproject.toml")
    files = {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(paths)}
    dependencies = {}
    for name in ("torch", "numpy", "sentence-transformers", "transformers", "huggingface-hub"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    identity = {"files": files, "python": platform.python_version(), "dependencies": dependencies}
    return {"sha256": _canonical_sha256(identity), **identity}


def _git(repository_root: Path) -> dict[str, str | bool | None]:
    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-c", f"safe.directory={repository_root.as_posix()}", "-C", str(repository_root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    commit = run("rev-parse", "HEAD")
    branch = run("branch", "--show-current") if commit else None
    status = run("status", "--porcelain") if commit else None
    return {"commit": commit, "branch": branch or None, "dirty": bool(status) if commit else None}


@dataclass(frozen=True, slots=True)
class RunManifest:
    schema_version: str
    run_id: str
    dataset_name: str
    dataset_version: str
    dataset_path: str
    dataset_sha256: str
    configuration: Mapping[str, Any]
    configuration_sha256: str
    environment: Mapping[str, str]
    git: Mapping[str, str | bool | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_run_manifest(
    *,
    run_id: str,
    dataset_name: str,
    dataset_version: str,
    dataset_path: str | Path,
    configuration: Mapping[str, Any],
    repository_root: str | Path,
) -> RunManifest:
    path = Path(dataset_path).resolve()
    config = dict(configuration)
    return RunManifest(
        schema_version="dive-eval-manifest-v1",
        run_id=run_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        dataset_path=str(path),
        dataset_sha256=sha256_file(path),
        configuration=config,
        configuration_sha256=_canonical_sha256(config),
        environment={
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        git=_git(Path(repository_root).resolve()),
    )
