-- Transactional PostgreSQL worker primitives. These functions keep claim,
-- retry and completion semantics next to the schema so independent workers do
-- not implement subtly different locking rules.

CREATE OR REPLACE FUNCTION claim_memory_outbox(batch_size integer DEFAULT 100,
                                                max_attempts integer DEFAULT 3)
RETURNS TABLE(event_id text)
LANGUAGE sql
AS $$
  UPDATE outbox
     SET status = 'FAILED', attempts = attempts + 1,
         last_error = COALESCE(last_error, 'stale worker claim'), claimed_at = NULL
   WHERE status = 'PROCESSING'
     AND claimed_at < now() - interval '5 minutes';

  WITH candidates AS (
    SELECT o.event_id
    FROM outbox o
    WHERE o.status IN ('PENDING', 'FAILED')
      AND o.attempts < max_attempts
    ORDER BY o.created_at, o.event_id
    FOR UPDATE SKIP LOCKED
    LIMIT GREATEST(1, LEAST(batch_size, 1000))
  ), claimed AS (
    UPDATE outbox o
       SET status = 'PROCESSING', claimed_at = now(), last_error = NULL
      FROM candidates c
     WHERE o.event_id = c.event_id
    RETURNING o.event_id
  )
  SELECT claimed.event_id FROM claimed;
$$;

CREATE OR REPLACE FUNCTION complete_memory_outbox(target_event_id text)
RETURNS void
LANGUAGE sql
AS $$
  UPDATE outbox
     SET status = 'DONE', processed_at = now(), claimed_at = NULL, last_error = NULL
   WHERE event_id = target_event_id;
$$;

CREATE OR REPLACE FUNCTION fail_memory_outbox(target_event_id text, error_message text)
RETURNS void
LANGUAGE sql
AS $$
  UPDATE outbox
     SET status = 'FAILED', attempts = attempts + 1,
         last_error = left(error_message, 2000), claimed_at = NULL
   WHERE event_id = target_event_id;
$$;
