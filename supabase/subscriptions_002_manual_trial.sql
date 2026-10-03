-- Migration for databases that already ran the first subscriptions.sql (which auto-started
-- a trial for every user). After this, new users choose between "Start free trial" and paying.
-- Safe to re-run. Existing rows and their trial dates are left exactly as they are.

DROP TRIGGER IF EXISTS on_auth_user_created_trial ON auth.users;
DROP FUNCTION IF EXISTS create_trial_subscription();

ALTER TABLE subscriptions ALTER COLUMN trial_start_date DROP NOT NULL;
ALTER TABLE subscriptions ALTER COLUMN trial_start_date DROP DEFAULT;
ALTER TABLE subscriptions ALTER COLUMN trial_end_date DROP NOT NULL;

ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_status_check;
ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_status_check
    CHECK (status IN ('NEW', 'TRIAL', 'PENDING', 'ACTIVE', 'PAYMENT_FAILED', 'PAUSED', 'CANCELLED', 'EXPIRED'));
ALTER TABLE subscriptions ALTER COLUMN status SET DEFAULT 'NEW';
