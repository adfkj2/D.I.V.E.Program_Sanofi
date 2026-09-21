-- Indexed canonical keys prevent relationship resolution from scanning every
-- active memory in a namespace on each ingest.
CREATE TABLE IF NOT EXISTS memory_keys (
  memory_id text PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  namespace text NOT NULL,
  subject_key text NOT NULL,
  predicate_key text NOT NULL,
  value_key text NOT NULL
);
INSERT INTO memory_keys(memory_id,namespace,subject_key,predicate_key,value_key)
SELECT id,namespace,
       lower(COALESCE(structured_content->>'subject','user')),
       lower(COALESCE(structured_content->>'predicate','statement')),
       lower(COALESCE(structured_content->>'normalized_value',structured_content->>'value',content))
FROM memories
ON CONFLICT(memory_id) DO NOTHING;
CREATE INDEX IF NOT EXISTS memory_keys_lookup_idx
  ON memory_keys(namespace, subject_key, predicate_key, value_key);
