# Deletion semantics

## Soft delete

`forget(memory_id)` creates a namespaced, content-free tombstone, marks the
memory `DELETED`, and immediately removes FTS, active vector state/staging,
entity links and relation links. The source event and memory row remain for
audit/recovery. Retrieval always applies deletion/status filtering.

## Hard memory purge

`forget(memory_id, hard=True)` additionally removes the memory row, sources,
versions, transitions, access rows and all derived projections. It retains only
the namespaced tombstone id/reason/time. The operation is irreversible inside
the active SQLite database.

## Event deletion

Soft event deletion tombstones the event and retires memories for which it is
the sole source. Shared memories retain other provenance. Hard event purge also
removes the source event, outbox/write-decision rows and event-sourced relation
records, reassigning shared relation provenance where possible.

## Scope and limitations

The verified active-store boundary now includes SQLite plus a real PostgreSQL
integration test. PostgreSQL hard memory purge checks the memory row, vector,
source, version, key, access, entity/relation links, transition references and
source-event relation residue while retaining the content-free tombstone.

Exports, filesystem snapshots, database backups, telemetry sinks,
model-provider logs and user-held copies are not managed by these methods. A
production deletion SLA must enumerate those systems, backup retention and
legal-hold precedence. PostgreSQL RLS, backup erasure and restore-after-purge
remain unverified, so this is an active-database guarantee rather than an
end-to-end regulatory erasure claim.
