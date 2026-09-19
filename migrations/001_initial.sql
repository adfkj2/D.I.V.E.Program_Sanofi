-- PostgreSQL reference schema. The local adapter uses the equivalent SQLite
-- schema in dive_memory.store; projections and indexes remain rebuildable.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS events (
  id text PRIMARY KEY,
  namespace text NOT NULL,
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  observed_at timestamptz NOT NULL,
  occurred_from timestamptz,
  occurred_to timestamptz,
  source_message_id text,
  idempotency_key text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(namespace, idempotency_key)
);
CREATE TABLE IF NOT EXISTS outbox (
  event_id text PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  processed_at timestamptz
);
CREATE TABLE IF NOT EXISTS memories (
  id text PRIMARY KEY,
  namespace text NOT NULL,
  kind text NOT NULL,
  evidence_state text NOT NULL,
  content text NOT NULL,
  structured_content jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL,
  importance real NOT NULL,
  confidence real NOT NULL,
  salience real NOT NULL,
  durability text NOT NULL,
  valid_window tstzrange,
  observed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  supersedes_id text,
  contradicts_id text,
  model_version text,
  extractor_version text,
  search_document tsvector GENERATED ALWAYS AS
    (to_tsvector('simple', content || ' ' || structured_content::text)) STORED
);
CREATE TABLE IF NOT EXISTS memory_sources (
  memory_id text NOT NULL REFERENCES memories(id),
  event_id text NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  PRIMARY KEY(memory_id, event_id)
);
CREATE TABLE IF NOT EXISTS memory_access (
  id bigserial PRIMARY KEY,
  memory_id text NOT NULL REFERENCES memories(id),
  query text NOT NULL,
  rank integer NOT NULL,
  score real NOT NULL,
  used boolean NOT NULL DEFAULT false,
  accessed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS memory_versions (
  id bigserial PRIMARY KEY,
  memory_id text NOT NULL REFERENCES memories(id),
  version integer NOT NULL,
  snapshot jsonb NOT NULL,
  reason text NOT NULL,
  source_event_id text REFERENCES events(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(memory_id, version)
);
CREATE TABLE IF NOT EXISTS write_decisions (
  event_id text NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  candidate_index integer NOT NULL,
  accepted boolean NOT NULL,
  importance real NOT NULL,
  confidence real NOT NULL,
  salience real NOT NULL,
  durability text NOT NULL,
  reason text NOT NULL,
  extractor_version text NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(event_id, candidate_index)
);
CREATE TABLE IF NOT EXISTS memory_vectors (
  memory_id text PRIMARY KEY REFERENCES memories(id),
  embedding vector NOT NULL,
  model text NOT NULL,
  dimensions integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tombstones (
  object_type text NOT NULL,
  object_id text NOT NULL,
  namespace text NOT NULL,
  reason text NOT NULL,
  deleted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (object_type, object_id)
);
CREATE TABLE IF NOT EXISTS entities (
  id bigserial PRIMARY KEY,
  namespace text NOT NULL,
  canonical_name text NOT NULL,
  entity_type text NOT NULL,
  UNIQUE(namespace, canonical_name)
);
CREATE TABLE IF NOT EXISTS memory_entities (
  memory_id text NOT NULL REFERENCES memories(id),
  entity_id bigint NOT NULL REFERENCES entities(id),
  role text NOT NULL,
  PRIMARY KEY(memory_id, entity_id)
);
CREATE TABLE IF NOT EXISTS relations (
  id bigserial PRIMARY KEY,
  namespace text NOT NULL,
  subject_entity_id bigint NOT NULL REFERENCES entities(id),
  predicate text NOT NULL,
  object_entity_id bigint NOT NULL REFERENCES entities(id),
  valid_window tstzrange,
  confidence real NOT NULL,
  source_event_id text NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  UNIQUE(namespace, subject_entity_id, predicate, object_entity_id, source_event_id)
);
CREATE TABLE IF NOT EXISTS memory_relations (
  memory_id text NOT NULL REFERENCES memories(id),
  relation_id bigint NOT NULL REFERENCES relations(id),
  PRIMARY KEY(memory_id, relation_id)
);
CREATE TABLE IF NOT EXISTS profiles (
  namespace text PRIMARY KEY,
  schema_version integer NOT NULL DEFAULT 1,
  current_json jsonb NOT NULL DEFAULT '{}',
  generated_from_version integer NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS memory_modes (
  namespace text PRIMARY KEY,
  enabled boolean NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jobs (
  id text PRIMARY KEY,
  job_type text NOT NULL,
  namespace text,
  status text NOT NULL CHECK (status IN ('RUNNING', 'DONE', 'FAILED')),
  dry_run boolean NOT NULL DEFAULT false,
  output_summary jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS memories_scope_status_idx ON memories(namespace, status);
CREATE INDEX IF NOT EXISTS memories_valid_window_gist ON memories USING gist(valid_window);
CREATE INDEX IF NOT EXISTS memories_search_document_idx ON memories USING gin(search_document);
CREATE INDEX IF NOT EXISTS memory_sources_event_idx ON memory_sources(event_id);
CREATE INDEX IF NOT EXISTS memory_versions_memory_idx ON memory_versions(memory_id, version);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox(status, created_at) WHERE status IN ('PENDING', 'FAILED');
CREATE INDEX IF NOT EXISTS tombstones_deleted_at_idx ON tombstones(deleted_at);
CREATE INDEX IF NOT EXISTS tombstones_scope_deleted_idx ON tombstones(namespace, deleted_at);
CREATE INDEX IF NOT EXISTS entities_scope_name_idx ON entities(namespace, canonical_name);
CREATE INDEX IF NOT EXISTS relations_scope_predicate_idx ON relations(namespace, predicate);
CREATE INDEX IF NOT EXISTS jobs_scope_created_idx ON jobs(namespace, created_at DESC);
