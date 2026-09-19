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
  idempotency_key text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
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
  contradicts_id text
);
CREATE TABLE IF NOT EXISTS memory_sources (
  memory_id text NOT NULL REFERENCES memories(id),
  event_id text NOT NULL REFERENCES events(id),
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
  source_event_id text NOT NULL REFERENCES events(id),
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
CREATE INDEX IF NOT EXISTS memories_scope_status_idx ON memories(namespace, status);
CREATE INDEX IF NOT EXISTS memories_valid_window_gist ON memories USING gist(valid_window);
CREATE INDEX IF NOT EXISTS memory_sources_event_idx ON memory_sources(event_id);
CREATE INDEX IF NOT EXISTS entities_scope_name_idx ON entities(namespace, canonical_name);
CREATE INDEX IF NOT EXISTS relations_scope_predicate_idx ON relations(namespace, predicate);
