# Backup, Restore and Retention Policy

Status: **policy defined, restore drill not yet performed.** The scripts in this
directory (`backup.sh`, `restore.sh`) implement the mechanism; the first
**restore drill must be recorded as a dated artifact before any production
claim** depends on this document. Per `docs/22` §5, no restore means no
erasure claim beyond the live database.

## Scope and honest boundary

- Backups cover the **PostgreSQL** store only. The default SQLite file
  (`memory.db`) is backed up by whatever filesystem/backup discipline covers
  its host directory; this repository defines none.
- `pg_dump --format=custom` is a **logical** backup: it protects against data
  loss, not against a corrupted cluster (use `pg_basebackup` for physical).
- Deletion inside the live database (hard purge) does **not** propagate into
  existing backups. Deleted personal data survives in every backup taken
  before the deletion. This is stated plainly because `docs/22` ranks
  erasure-beyond-the-database as the highest-exposure gap.

## Retention schedule

| Generation | Count | Kept |
|---|---|---|
| Daily | 14 | `backup.sh` default `KEEP=14` |
| Weekly | 4 | run `backup.sh` with `KEEP` unset on a weekly directory, prune to 4 |
| Monthly | 6 | same pattern, prune to 6 |

RPO target (provisional, not an attained SLO): **24 h** (daily cadence).
RTO target (provisional): **2 h**, to be measured by the first restore drill.

## Legal hold vs erasure precedence

1. A deletion request (hard purge of a memory/event) is executed in the live
   database immediately — that path is verified by the E3 tests.
2. Backups are **immutable**: the request does not rewrite past dumps.
3. When a legal hold is active for a namespace, remove that namespace's
   relevant backup files from the retention pruning candidates **before**
   `backup.sh` runs; the script prunes only its own `dive-backup-*` pattern,
   so out-of-pattern files are never touched.
4. A restore into a live environment re-exports previously deleted data.
   A restore performed for any reason other than disaster recovery must be
   followed by re-running the deletion, and this must be recorded.

## Restore drill (the missing PASS)

Before this policy can be marked verified, perform and record:

1. `backup.sh "$DSN" drill-dir 3` against a populated database.
2. Create an empty database and `restore.sh "$EMPTY_DSN" drill-dir/dive-backup-*.dump`.
3. Compare row counts per table between source and restored copy.
4. Restore a second time with `FORCE=1` over the drill target (idempotent path).
5. Write the commands, output, timings and row counts into a dated artifact
   under `artifacts/`, referenced from `docs/31`.

A drill that has not been recorded did not happen.
