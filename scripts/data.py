"""Offline consistent backup/restore. Stop the server before either operation."""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path


def backup(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {"format": "wot-local-backup-v2", "files": {}}
    for name in ("wot.db", "memory.db", "assessment-private.db"):
        path = source / name
        if not path.is_file():
            if name == "assessment-private.db":
                continue
            raise ValueError(f"missing {name}")
        with sqlite3.connect(str(path)) as src, sqlite3.connect(str(destination / name)) as dst:
            src.backup(dst)
        manifest["files"][name] = hashlib.sha256((destination / name).read_bytes()).hexdigest()
    for path in (source / "avatars").glob("*.png"):
        (destination / "avatars").mkdir(exist_ok=True)
        name = "avatars/" + path.name
        shutil.copyfile(path, destination / name)
        manifest["files"][name] = hashlib.sha256((destination / name).read_bytes()).hexdigest()
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def restore(source: Path, destination: Path) -> None:
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files", {})
    if manifest.get("format") not in {"wot-local-backup-v1", "wot-local-backup-v2"} or not {"wot.db", "memory.db"} <= set(files):
        raise ValueError("invalid backup manifest")
    if any(name not in {"wot.db", "memory.db", "assessment-private.db"} and not re.fullmatch(
        r"avatars/[0-9a-f]{64}\.png", name) for name in files):
        raise ValueError("invalid backup path")
    for name, expected in manifest["files"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"checksum mismatch: {name}")
    # A pre-deletion backup must not overwrite current tombstones. Restore into
    # an empty separate directory only and explicitly choose it on next startup.
    if destination.exists():
        raise ValueError("restore destination must be a new, empty directory")
    destination.mkdir(parents=True)
    for name in manifest["files"]:
        if name.endswith(".db"):
            with sqlite3.connect(str(source / name)) as src, sqlite3.connect(str(destination / name)) as dst:
                src.backup(dst)
        else:
            (destination / "avatars").mkdir(exist_ok=True)
            shutil.copyfile(source / name, destination / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stop WOT first; backups contain private data")
    parser.add_argument("operation", choices=["backup", "restore"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    {"backup": backup, "restore": restore}[args.operation](args.source, args.destination)
