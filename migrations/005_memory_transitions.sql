CREATE TABLE IF NOT EXISTS memory_transitions (
  id text PRIMARY KEY,
  namespace text NOT NULL,
  from_memory_id text,
  to_memory_id text,
  relationship text NOT NULL CHECK (relationship IN (
    'unrelated','duplicate','reinforcement','refinement','correction',
    'temporal_update','contradiction','supersession'
  )),
  action text NOT NULL CHECK (action IN ('CREATE','MERGE_PROVENANCE','REINFORCE','SUPERSEDE','COEXIST')),
  confidence real NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  reason text NOT NULL,
  source_event_id text NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  resolver_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(namespace, from_memory_id, to_memory_id, relationship, source_event_id)
);
CREATE INDEX IF NOT EXISTS memory_transitions_scope_idx
    ON memory_transitions(namespace, created_at);
