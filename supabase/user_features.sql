-- Per-user preferences and push-notification tokens. Run once in the Supabase SQL editor (after schema.sql and
-- reel_jobs.sql) BEFORE deploying the backend version that uses them. Safe to re-run.
--
-- Backend-only: RLS on with no policies (service-role key only). Both tables cascade away with the user, so account
-- deletion (auth.users row removed by the backend) leaves nothing behind.

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    -- "Faster Search" switch. Only honoured while the user is entitled to it (paid plan); the backend decides.
    faster_search BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS push_tokens (
    token TEXT PRIMARY KEY,                    -- Expo push token of one app install
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    platform TEXT NOT NULL DEFAULT 'android',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS push_tokens_user_idx ON push_tokens (user_id);

ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE push_tokens ENABLE ROW LEVEL SECURITY;
