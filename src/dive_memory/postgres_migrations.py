from __future__ import annotations

from pathlib import Path
from typing import Any


def migration_files(root: str | Path) -> list[Path]:
    files = sorted(Path(root).glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations found under {root}")
    versions = [path.name.split("_", 1)[0] for path in files]
    if len(versions) != len(set(versions)):
        raise ValueError("migration versions must be unique")
    return files


def apply_migrations(connection: Any, root: str | Path) -> list[str]:
    """Apply idempotent reference migrations and record their checksums."""

    from hashlib import sha256

    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version text PRIMARY KEY, filename text NOT NULL, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied: list[str] = []
    for path in migration_files(root):
        version = path.name.split("_", 1)[0]
        sql = path.read_text(encoding="utf-8")
        checksum = sha256(sql.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=%s", (version,)
        ).fetchone()
        if row:
            if row[0] != checksum:
                raise RuntimeError(f"applied migration {version} checksum changed")
            continue
        connection.execute(sql)
        connection.execute(
            "INSERT INTO schema_migrations(version,filename,sha256) VALUES (%s,%s,%s)",
            (version, path.name, checksum),
        )
        applied.append(path.name)
    connection.commit()
    return applied
