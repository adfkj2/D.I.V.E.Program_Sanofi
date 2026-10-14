#!/usr/bin/env bash
# Verified restore of a backup produced by backup.sh.
# Refuses to restore into a non-empty database unless FORCE=1 is set.
#
# Usage: restore.sh DSN BACKUP.dump
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 DSN BACKUP.dump" >&2
  exit 2
fi

dsn=$1
dump=$2

command -v pg_restore >/dev/null || { echo "pg_restore not found" >&2; exit 1; }
[ -f "$dump" ] || { echo "no such file: $dump" >&2; exit 1; }
[ -f "$dump.sha256" ] || { echo "missing checksum sidecar: $dump.sha256" >&2; exit 1; }

if command -v sha256sum >/dev/null; then
  (cd "$(dirname "$dump")" && sha256sum --check "$(basename "$dump").sha256")
else
  (cd "$(dirname "$dump")" && shasum -a 256 --check "$(basename "$dump").sha256")
fi

tables=$(psql "$dsn" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
if [ "$tables" != "0" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "target database is not empty ($tables tables); set FORCE=1 to overwrite" >&2
  exit 1
fi

pg_restore --clean --if-exists --dbname="$dsn" "$dump"
echo "restore complete; verify with: psql '$dsn' -c '\\dt'"
