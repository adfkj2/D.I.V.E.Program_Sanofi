-- Generation-aware vectors allow resumable backfill and atomic model cutover.
CREATE TABLE IF NOT EXISTS embedding_generations (
  id text PRIMARY KEY,
  provider text NOT NULL,
  model text NOT NULL,
  revision text NOT NULL,
  dimensions integer NOT NULL,
  status text NOT NULL CHECK (status IN ('PENDING','READY','ACTIVE','RETIRED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  activated_at timestamptz
);
CREATE TABLE IF NOT EXISTS embedding_state (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  active_generation text REFERENCES embedding_generations(id),
  previous_generation text REFERENCES embedding_generations(id)
);
ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS generation_id text REFERENCES embedding_generations(id);
ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS revision text;
ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS degraded boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS memory_vectors_generation_idx ON memory_vectors(generation_id, memory_id);

-- Existing vectors intentionally remain generation_id=NULL until an explicit
-- backfill assigns a pinned model revision. Production readiness must fail
-- while any active vector lacks a generation.
