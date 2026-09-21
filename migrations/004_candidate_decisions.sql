-- Phase 3 extraction/gate audit metadata for an existing PostgreSQL database.
ALTER TABLE write_decisions ADD COLUMN IF NOT EXISTS outcome_code text NOT NULL DEFAULT 'COMMITTED';
ALTER TABLE write_decisions ADD COLUMN IF NOT EXISTS features_json jsonb NOT NULL DEFAULT '{}';
ALTER TABLE write_decisions ADD COLUMN IF NOT EXISTS policy_version text NOT NULL DEFAULT 'utility-baseline-v1';
ALTER TABLE write_decisions ADD COLUMN IF NOT EXISTS prompt_version text;
ALTER TABLE write_decisions ADD COLUMN IF NOT EXISTS model_version text;
ALTER TABLE write_decisions ADD COLUMN IF NOT EXISTS schema_version text;
