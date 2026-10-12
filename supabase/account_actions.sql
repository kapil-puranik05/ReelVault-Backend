-- Account deletion notices + admin audit log. Run once in the Supabase SQL editor BEFORE deploying the backend
-- version that uses them. Safe to re-run. Backend-only: RLS on with no policies (service-role key only).

-- When an account is deleted we keep ONE thing: a keyed hash of its email, so that a person who later tries to sign
-- in can be told "this account was deleted" instead of a confusing "wrong password". The email itself is not kept,
-- and the row carries no profile data. (Suspended accounts need no row: Supabase Auth itself refuses their sign-in.)
CREATE TABLE IF NOT EXISTS deleted_accounts (
    email_hash TEXT PRIMARY KEY,          -- HMAC-SHA256 of the lower-cased email, keyed with a server secret
    user_id UUID NOT NULL,                -- the former account id (no foreign key: the user no longer exists)
    deleted_by TEXT NOT NULL CHECK (deleted_by IN ('admin', 'user')),
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Who did what to whose account, from the admin dashboard. target_user_id is not a foreign key so the record
-- outlives a deleted user.
CREATE TABLE IF NOT EXISTS admin_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor TEXT NOT NULL,                  -- the admin's email
    action TEXT NOT NULL CHECK (action IN ('suspend', 'unsuspend', 'delete')),
    target_user_id UUID NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS admin_actions_created_idx ON admin_actions (created_at DESC);
CREATE INDEX IF NOT EXISTS admin_actions_target_idx ON admin_actions (target_user_id);

ALTER TABLE deleted_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_actions ENABLE ROW LEVEL SECURITY;
