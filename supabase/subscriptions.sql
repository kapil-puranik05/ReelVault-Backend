-- Subscription / trial / Razorpay schema. Run once in the Supabase SQL editor
-- (after schema.sql). Safe to re-run: everything is IF NOT EXISTS.
-- Already ran an earlier version of this file? Run subscriptions_002_manual_trial.sql instead.

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    -- NEW (no plan chosen yet) | TRIAL | PENDING | ACTIVE | PAYMENT_FAILED | PAUSED | CANCELLED | EXPIRED
    status TEXT NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW', 'TRIAL', 'PENDING', 'ACTIVE', 'PAYMENT_FAILED', 'PAUSED', 'CANCELLED', 'EXPIRED')),
    plan TEXT NOT NULL DEFAULT 'FREE_TRIAL' CHECK (plan IN ('FREE_TRIAL', 'PREMIUM')),
    -- NULL until the user taps "Start free trial" (POST /api/subscription/start-trial).
    trial_start_date TIMESTAMPTZ,
    trial_end_date TIMESTAMPTZ,
    subscription_start_date TIMESTAMPTZ,
    -- End of the current paid billing period (Razorpay current_end). Next renewal while ACTIVE.
    subscription_end_date TIMESTAMPTZ,
    razorpay_subscription_id TEXT UNIQUE,
    razorpay_customer_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Razorpay plans are immutable; one row per (amount, currency) we have created,
-- so changing PAID_PLAN_PRICE transparently creates a new plan.
CREATE TABLE IF NOT EXISTS billing_plans (
    id TEXT PRIMARY KEY,               -- Razorpay plan id
    amount_minor INT NOT NULL,         -- paise for INR
    currency TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT 'monthly',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (amount_minor, currency, period)
);

-- Webhook idempotency: an event id is inserted before processing; a duplicate insert = already handled.
CREATE TABLE IF NOT EXISTS razorpay_events (
    id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Server-only tables: RLS on with no policies = no access for anon/authenticated
-- roles; the backend uses the service role key, which bypasses RLS.
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE razorpay_events ENABLE ROW LEVEL SECURITY;

-- No trigger and no backfill: the backend creates an empty row (status NEW, no trial)
-- the first time a user opens the app, and the trial only starts when they choose it.
