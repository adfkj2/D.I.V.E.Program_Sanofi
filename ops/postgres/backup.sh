#!/usr/bin/env bash
# Logical backup of a DIVE PostgreSQL database in custom format, with a
# SHA-256 sidecar so restores can be verified before touching the target.
#
# Usage: backup.sh DSN OUTPUT_DIR [KEEP]
#   DSN         connection string of the database to back up
#   OUTPUT_DIR  directory that receives dive-backup-<utc>.dump(+.sha256)
#   KEEP        number of newest backups to retain (default 14); older
#               files are deleted by this script's own naming pattern only
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 DSN OUTPUT_DIR [KEEP]" >&2
  exit 2
fi

dsn=$1
outdir=$2
keep=${3:-14}

command -v pg_dump >/dev/null || { echo "pg_dump not found" >&2; exit 1; }
command -v sha256sum >/dev/null || command -v shasum >/dev/null || {
  echo "sha256 tool not found" >&2
  exit 1
}

mkdir -p "$outdir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$outdir/dive-backup-$stamp.dump"

pg_dump --format=custom --file="$target" "$dsn"

if command -v sha256sum >/dev/null; then
  sha256sum "$target" >"$target.sha256"
else
  shasum -a 256 "$target" >"$target.sha256"
fi

echo "backup written: $target"
ls -l "$target" "$target.sha256"

# Retention: prune only files matching this script's own naming pattern.
# Files under legal hold must be removed from the pruning candidate list
# (see BACKUP_POLICY.md) before this loop runs.
mapfile -t candidates < <(ls -1 "$outdir"/dive-backup-*.dump 2>/dev/null | sort)
if [ "${#candidates[@]}" -gt "$keep" ]; then
  for stale in "${candidates[@]::-${keep}}"; do
    rm -f -- "$stale" "$stale.sha256"
    echo "retention: removed $stale"
  done
fi
