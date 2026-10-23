"""Preview a fixed cache allow-list; archive old pytest data before removing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
CACHES = frozenset(
    {".pytest_cache", ".ruff_cache", ".mypy_cache", ".npm-cache", "build"}
)


def _link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _files(target: Path, root: Path, tracked: set[str]) -> dict[str, str]:
    if _link(target) or target.resolve().parent != root:
        raise ValueError("target is a link or escapes the repository root")
    result = {}

    def fail(error: OSError) -> None:
        raise error

    for parent, directories, names in os.walk(target, followlinks=False, onerror=fail):
        for name in [*directories, *names]:
            path = Path(parent) / name
            if _link(path):
                raise ValueError("cache contains a link or Windows reparse point")
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in tracked:
                raise ValueError("cache contains a tracked file")
            if path.is_file():
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _tracked(root: Path) -> set[str]:
    # Conservative on all platforms, including case-only Windows renames.
    return {
        name.casefold()
        for name in (
            subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
            .decode("utf-8")
            .strip("\0")
            .split("\0")
        )
    }


def plan_cleanup(root: Path) -> dict:
    root = root.resolve(strict=True)
    git_root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=root, text=True
        ).strip()
    ).resolve()
    if git_root != root or not (root / "pyproject.toml").is_file():
        raise ValueError("cleanup requires the exact project repository root")
    tracked = _tracked(root)
    targets, blocked = [], []
    for path in sorted(root.iterdir()):
        temporary = path.name.startswith(".pytest-")
        if path.name not in CACHES and not temporary and path.name != "deploy":
            continue
        try:
            if not path.is_dir() or _link(path):
                raise ValueError("candidate is not a regular directory")
            files = _files(path, root, tracked)
            if path.name == "deploy" and any(path.iterdir()):
                continue
            targets.append(
                {
                    "path": path.name,
                    "archive": temporary,
                    "files": files,
                    "bytes": sum((root / name).stat().st_size for name in files),
                }
            )
        except (OSError, ValueError) as exc:
            blocked.append({"path": path.name, "reason": type(exc).__name__})
    return {"root": str(root), "targets": targets, "blocked": blocked}


def apply_cleanup(plan: dict, output: Path) -> dict:
    """Revalidate before deletion. Refuse changed files, links and occupied output."""
    root = Path(plan["root"])
    output = output.resolve()
    if not output.is_relative_to(root / "eval" / "reports"):
        raise ValueError("cleanup records must stay under eval/reports")
    output.mkdir(parents=True, exist_ok=False)
    current = plan_cleanup(root)
    if current != plan:
        raise ValueError("workspace changed since preview; nothing was deleted")
    archive = output / "pytest-temporaries.zip"
    archived = {
        name: digest
        for t in plan["targets"]
        if t["archive"]
        for name, digest in t["files"].items()
    }
    if archived:
        with ZipFile(archive, "x", compression=ZIP_DEFLATED) as bundle:
            for name in archived:
                bundle.write(root / name, name)
        with ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("archive CRC check failed; nothing was deleted")
            for name, digest in archived.items():
                if hashlib.sha256(bundle.read(name)).hexdigest() != digest:
                    raise ValueError("archive hash check failed; nothing was deleted")
    # The source files may have changed while the archive was being written.
    if plan_cleanup(root) != plan:
        raise ValueError("workspace changed during archiving; nothing was deleted")
    deleted, errors = [], []
    for target in plan["targets"]:
        path = root / target["path"]
        try:
            # Resolve and check the final absolute target immediately before rmtree.
            if _link(path) or path.resolve().parent != root:
                raise ValueError("target no longer belongs to this repository")
            if _files(path, root, _tracked(root)) != target["files"]:
                raise ValueError("target changed after archiving; keeping its contents")
            shutil.rmtree(path)
            deleted.append(target["path"])
        except (OSError, ValueError) as exc:
            errors.append({"path": target["path"], "reason": type(exc).__name__})
    result = {
        **plan,
        "deleted": deleted,
        "errors": errors,
        "archive": archive.relative_to(root).as_posix() if archived else None,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()
        if archived
        else None,
        "deleted_bytes": sum(
            t["bytes"] for t in plan["targets"] if t["path"] in deleted
        ),
    }
    (output / "cleanup.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.apply and args.output is None:
        parser.error("--apply requires a new --output directory under eval/reports")
    plan = plan_cleanup(ROOT)
    result = apply_cleanup(plan, args.output) if args.apply else plan
    print(
        json.dumps(
            {
                "preview": not args.apply,
                "targets": [
                    {k: v for k, v in t.items() if k != "files"}
                    for t in result["targets"]
                ],
                "blocked": result["blocked"],
                **(
                    {
                        k: result[k]
                        for k in ("deleted", "errors", "deleted_bytes", "archive")
                    }
                    if args.apply
                    else {}
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(bool(result.get("errors")))


if __name__ == "__main__":
    raise SystemExit(main())
