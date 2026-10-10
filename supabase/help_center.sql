-- Help Center support queries. Run once in the Supabase SQL editor (after schema.sql).
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS support_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,
    user_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS support_queries_user_idx ON support_queries (user_id, created_at DESC);

-- Server-only table: RLS on with no policies; the backend uses the service role key.
ALTER TABLE support_queries ENABLE ROW LEVEL SECURITY;
