-- Scope deletion audit rows so exports remain tenant-safe after the source
-- event and memory content have been hard-purged.

ALTER TABLE tombstones ADD COLUMN IF NOT EXISTS namespace text;

UPDATE tombstones t
   SET namespace = e.namespace
  FROM events e
 WHERE t.object_type = 'event'
   AND t.object_id = e.id
   AND t.namespace IS NULL;

UPDATE tombstones t
   SET namespace = m.namespace
  FROM memories m
 WHERE t.object_type = 'memory'
   AND t.object_id = m.id
   AND t.namespace IS NULL;

-- Legacy tombstones whose content was already purged cannot be attributed
-- safely. They remain NULL and are intentionally excluded from scoped export.
CREATE INDEX IF NOT EXISTS tombstones_scope_deleted_idx
    ON tombstones(namespace, deleted_at);
