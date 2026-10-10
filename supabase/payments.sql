-- Payments ledger + Razorpay test/live separation. Run once in the Supabase SQL editor
-- (after subscriptions.sql) BEFORE deploying the backend version that writes to it.
-- Safe to re-run.
--
-- Backend-only: RLS is on with NO policies, so the app (anon / authenticated roles) can never read or
-- write these rows; only the backend's service-role key can. Card/UPI/bank details are never stored.

CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,                       -- Razorpay payment id (pay_...); also the idempotency key
    -- Kept (set to NULL) if the user is deleted: financial records outlive the account.
    user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    razorpay_subscription_id TEXT,
    razorpay_order_id TEXT,
    razorpay_invoice_id TEXT,
    amount_minor INT NOT NULL,                 -- paise for INR
    currency TEXT NOT NULL,
    status TEXT NOT NULL,                      -- Razorpay: created | authorized | captured | refunded | failed
    method TEXT,                               -- card | upi | netbanking | wallet ... (no instrument details)
    fee_minor INT,                             -- Razorpay's fee, paise
    tax_minor INT,                             -- GST on that fee, paise
    amount_refunded_minor INT NOT NULL DEFAULT 0,
    error_code TEXT,                           -- failed payments only
    error_description TEXT,
    livemode BOOLEAN NOT NULL,                 -- false = recorded while the backend ran on rzp_test_ keys
    razorpay_created_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS payments_user_idx ON payments (user_id, razorpay_created_at DESC);
CREATE INDEX IF NOT EXISTS payments_subscription_idx ON payments (razorpay_subscription_id);

ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

-- Test and live Razorpay accounts have separate plans and subscriptions. Tag which one each cached
-- plan / user checkout belongs to, so live keys never reuse a test-mode id (Razorpay would reject it).
-- Existing rows were all created with test keys, hence the 'test' default.
ALTER TABLE billing_plans ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'test'
    CHECK (mode IN ('test', 'live'));
-- Replace the old UNIQUE (amount_minor, currency, period) with one that includes mode. Found by type, not
-- by its auto-generated name, so it works whatever the constraint ended up being called.
DO $$
DECLARE c RECORD;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'billing_plans'::regclass AND contype = 'u' LOOP
        EXECUTE format('ALTER TABLE billing_plans DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS billing_plans_amount_currency_period_mode_key
    ON billing_plans (amount_minor, currency, period, mode);

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS razorpay_mode TEXT NOT NULL DEFAULT 'test'
    CHECK (razorpay_mode IN ('test', 'live'));

-- NOT run automatically — a business decision. Users who "paid" during testing (status ACTIVE on a test
-- subscription) keep premium access forever, because live webhooks will never update their test
-- subscription. Review, then run by hand if you want test-mode subscribers back on the paywall:
--
--   UPDATE subscriptions
--      SET status = 'EXPIRED', plan = 'FREE_TRIAL', razorpay_subscription_id = NULL,
--          subscription_start_date = NULL, subscription_end_date = NULL, updated_at = NOW()
--    WHERE razorpay_mode = 'test' AND status IN ('ACTIVE', 'PENDING', 'PAYMENT_FAILED', 'PAUSED', 'CANCELLED');
