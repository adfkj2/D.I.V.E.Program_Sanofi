"""Hash-index historical reports without moving, deleting or committing every file."""

import gzip
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    rows, errors, excluded = [], [], []
    source = ROOT / "eval/reports"
    for directory, dirs, names in os.walk(source, onerror=lambda e: errors.append(type(e).__name__)):
        for name in list(dirs):
            if name.startswith(("pytest", ".pytest", "engineering-delivery-20261012")) or name in {"__pycache__", "environment"}:
                excluded.append((Path(directory) / name).relative_to(ROOT).as_posix())
                dirs.remove(name)
        for name in names:
            path = Path(directory) / name
            try:
                raw = path.read_bytes()
                rows.append({"path": path.relative_to(ROOT).as_posix(), "bytes": len(raw),
                             "sha256": hashlib.sha256(raw).hexdigest()})
            except OSError as exc:
                errors.append({"path": path.relative_to(ROOT).as_posix(), "error": type(exc).__name__})
    rows.sort(key=lambda r: r["path"])
    report = {"files": len(rows), "bytes": sum(r["bytes"] for r in rows), "errors": errors,
        "excluded_temporary_or_current_run_directories": excluded, "original_files_modified": False,
        "entries": rows}
    target = ROOT / "artifacts/history-index.json.gz"
    if target.exists():
        raise SystemExit("Index already exists; preserve it and use a new version")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(gzip.compress(json.dumps(report,ensure_ascii=False).encode(), mtime=0))
    print(json.dumps({k:v for k,v in report.items() if k != "entries"}))


if __name__ == "__main__":
    main()
