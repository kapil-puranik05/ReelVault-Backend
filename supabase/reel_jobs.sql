-- Background reel-saving queue. Run once in the Supabase SQL editor BEFORE deploying the backend version
-- that uses it. Safe to re-run.
--
-- A shared link becomes a row here in well under a second; the backend then runs the (slow) AI pipeline on its
-- own, so the phone never has to stay connected. Backend-only: RLS on with no policies (service-role key only).

CREATE TABLE IF NOT EXISTS reel_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'PROCESSING', 'DONE', 'FAILED')),
    reel_id UUID,                      -- the saved reel, once DONE (no FK: the user may delete the reel later)
    error TEXT,                        -- internal detail for FAILED; never sent to the app
    attempts INT NOT NULL DEFAULT 0,
    notified_at TIMESTAMPTZ,           -- set once when the 'reel is ready' push was handed off; stops duplicates
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS reel_jobs_user_idx ON reel_jobs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS reel_jobs_status_idx ON reel_jobs (status, updated_at);
-- Sharing the same link twice (double tap, re-share) while it is still being processed must not create two jobs.
CREATE UNIQUE INDEX IF NOT EXISTS reel_jobs_one_active_per_url
    ON reel_jobs (user_id, url) WHERE status IN ('QUEUED', 'PROCESSING');

ALTER TABLE reel_jobs ENABLE ROW LEVEL SECURITY;
