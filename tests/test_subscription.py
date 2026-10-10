"""Subscription lifecycle tests. Supabase and Razorpay are replaced with in-memory fakes;
everything else (routes, guard, webhook signature check, state mapping) is the real code."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import razorpay_client
from api.deps import CurrentUser, get_current_user
from api.errors import ApiError
from api.main import app
from api.rate_limit import _buckets
from pipeline.config import settings
from pipeline.storage import payment_store, subscription_store, supabase_store

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
WEBHOOK_SECRET = "whsec_test"


class FakeDb:
    def __init__(self):
        self.subs: dict[str, dict] = {}
        self.plans: dict[tuple, str] = {}
        self.events: set[str] = set()
        self.payments: dict[str, dict] = {}

    def install(self, monkeypatch):
        db = self

        def get(user_id):
            return dict(db.subs[user_id]) if user_id in db.subs else None

        def get_by_razorpay_id(rid):
            return next((dict(s) for s in db.subs.values() if s.get("razorpay_subscription_id") == rid), None)

        def create_if_missing(user_id):
            db.subs.setdefault(user_id, {
                "user_id": user_id, "status": "NEW", "plan": "FREE_TRIAL",
                "trial_start_date": None, "trial_end_date": None,
                "subscription_start_date": None, "subscription_end_date": None,
                "razorpay_subscription_id": None, "razorpay_customer_id": None,
            })

        def start_trial_if_unstarted(user_id, start, end):
            row = db.subs[user_id]
            if row["trial_start_date"] is not None:
                return False
            row.update(trial_start_date=start.isoformat(), trial_end_date=end.isoformat())
            if row["status"] == "NEW":
                row["status"] = "TRIAL"
            return True

        def update(user_id, fields):
            db.subs[user_id].update(fields)

        def claim(user_id, new_id, expected, mode):
            if db.subs[user_id].get("razorpay_subscription_id") != expected:
                return False
            db.subs[user_id].update({"razorpay_subscription_id": new_id, "status": "PENDING", "razorpay_mode": mode})
            return True

        def record_event(eid, event):
            if eid in db.events:
                return False
            db.events.add(eid)
            return True

        monkeypatch.setattr(subscription_store, "get", get)
        monkeypatch.setattr(subscription_store, "get_by_razorpay_id", get_by_razorpay_id)
        monkeypatch.setattr(subscription_store, "create_if_missing", create_if_missing)
        monkeypatch.setattr(subscription_store, "start_trial_if_unstarted", start_trial_if_unstarted)
        monkeypatch.setattr(subscription_store, "update", update)
        monkeypatch.setattr(subscription_store, "claim_razorpay_subscription", claim)
        monkeypatch.setattr(subscription_store, "get_plan_id", lambda a, c, m, p="monthly": db.plans.get((a, c, m, p)))
        monkeypatch.setattr(subscription_store, "save_plan", lambda pid, a, c, m, p="monthly": db.plans.__setitem__((a, c, m, p), pid))
        monkeypatch.setattr(payment_store, "get", lambda pid: dict(db.payments[pid]) if pid in db.payments else None)
        monkeypatch.setattr(payment_store, "upsert", lambda row: db.payments.__setitem__(row["id"], dict(row)))
        monkeypatch.setattr(subscription_store, "record_event", record_event)
        monkeypatch.setattr(subscription_store, "forget_event", lambda eid: db.events.discard(eid))

    def set_trial(self, user_id, days_from_now, **extra):
        end = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        start = end - timedelta(days=7)
        self.subs[user_id] = {
            "user_id": user_id, "status": "TRIAL", "plan": "FREE_TRIAL",
            "trial_start_date": start.isoformat(), "trial_end_date": end.isoformat(),
            "subscription_start_date": None, "subscription_end_date": None,
            "razorpay_subscription_id": None, "razorpay_customer_id": None, **extra,
        }


class FakeRazorpay:
    def __init__(self):
        self.subscriptions: dict[str, dict] = {}
        self.created_plans: list[tuple[int, str]] = []
        self.created_subs = 0
        self.invoices: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.payment_note_updates: list[tuple[str, dict]] = []
        self.fail_note_updates = False

    def install(self, monkeypatch):
        rz = self

        def create_plan(amount_minor, currency):
            rz.created_plans.append((amount_minor, currency))
            return {"id": f"plan_{len(rz.created_plans)}"}

        def create_subscription(plan_id, total_count, user_id):
            rz.created_subs += 1
            sub = {"id": f"sub_{rz.created_subs}", "status": "created", "notes": {"user_id": user_id, "app": "reelvault"},
                   "short_url": f"https://rzp.io/i/sub_{rz.created_subs}", "plan_id": plan_id}
            rz.subscriptions[sub["id"]] = sub
            return dict(sub)

        monkeypatch.setattr(razorpay_client, "create_plan", create_plan)
        monkeypatch.setattr(razorpay_client, "create_subscription", create_subscription)
        monkeypatch.setattr(razorpay_client, "fetch_subscription", lambda sid: dict(rz.subscriptions[sid]))
        monkeypatch.setattr(razorpay_client, "fetch_invoice", lambda iid: dict(rz.invoices[iid]))
        monkeypatch.setattr(razorpay_client, "fetch_payment", lambda pid: dict(rz.payments[pid]))

        def update_payment_notes(pid, notes):
            if rz.fail_note_updates:
                raise ApiError(502, "provider down")
            rz.payment_note_updates.append((pid, dict(notes)))
            return {"id": pid, "notes": notes}

        monkeypatch.setattr(razorpay_client, "update_payment_notes", update_payment_notes)

    def activate(self, sid):
        now = int(datetime.now(timezone.utc).timestamp())
        self.subscriptions[sid].update(status="active", current_start=now, current_end=now + 30 * 86400, start_at=now)


@pytest.fixture
def env(monkeypatch):
    _buckets.clear()
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "secret")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "paid_plan_price", 1)
    db, rz = FakeDb(), FakeRazorpay()
    db.install(monkeypatch)
    rz.install(monkeypatch)
    monkeypatch.setattr(supabase_store, "list_reels", lambda user_id: [])
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=USER, email="a@b.c")
    yield db, rz, TestClient(app)
    app.dependency_overrides.clear()


def signed_webhook(client, event, entity, event_id="evt_1", payment=None, refund=None):
    payload = {"subscription": {"entity": entity}} if entity is not None else {}
    if payment is not None:
        payload["payment"] = {"entity": payment}
    if refund is not None:
        payload["refund"] = {"entity": refund}
    body = json.dumps({"event": event, "payload": payload}).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/api/subscription/webhook", content=body,
                       headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id})


# ── Trial ────────────────────────────────────────────────────────────────────

def test_new_user_has_no_access_and_no_trial_until_they_choose(env):
    db, _, client = env
    body = client.get("/api/subscription").json()
    assert not body["hasAccess"] and body["accessReason"] == "NONE" and body["status"] == "NEW"
    assert body["trial"]["status"] == "NOT_STARTED" and body["trial"]["durationDays"] == 7
    assert body["trial"]["startDate"] is None and body["trial"]["endDate"] is None
    assert client.get("/api/reels").status_code == 402
    assert client.get("/api/reels").json()["code"] == "SUBSCRIPTION_REQUIRED"


def test_start_trial_begins_a_backend_timed_7_day_trial(env):
    db, _, client = env
    body = client.post("/api/subscription/start-trial").json()
    assert body["hasAccess"] and body["accessReason"] == "TRIAL" and body["status"] == "TRIAL"
    assert body["trial"]["status"] == "ACTIVE" and body["trial"]["daysRemaining"] == 7
    assert client.get("/api/reels").status_code == 200


def test_trial_starts_only_once_and_cannot_be_extended_or_restarted(env):
    db, _, client = env
    client.post("/api/subscription/start-trial")
    first = dict(db.subs[USER])
    client.post("/api/subscription/start-trial")
    assert db.subs[USER]["trial_end_date"] == first["trial_end_date"]

    db.set_trial(USER, days_from_now=-1)  # trial over
    body = client.post("/api/subscription/start-trial").json()
    assert not body["hasAccess"] and body["trial"]["status"] == "EXPIRED"
    assert client.get("/api/reels").status_code == 402


def test_user_can_pay_without_ever_starting_the_trial(env):
    db, rz, client = env
    sid = client.post("/api/subscription/create").json()["subscriptionId"]
    assert client.get("/api/reels").status_code == 402
    rz.activate(sid)
    body = client.post("/api/subscription/verify").json()
    assert body["accessReason"] == "PAID" and body["trial"]["status"] == "NOT_STARTED"
    assert client.get("/api/reels").status_code == 200


def test_expired_trial_blocks_reels_api_with_subscription_required(env):
    db, _, client = env
    db.set_trial(USER, days_from_now=-1)
    for method, path in [("get", "/api/reels"), ("get", "/api/reels/search?q=x"),
                         ("post", "/api/reels/save"), ("delete", f"/api/reels/{USER}")]:
        res = getattr(client, method)(path, **({"json": {"url": "https://x.com/r"}} if method == "post" else {}))
        assert res.status_code == 402, path
        assert res.json()["code"] == "SUBSCRIPTION_REQUIRED" and "error" in res.json()


def test_expired_user_can_still_reach_account_and_subscription_endpoints(env):
    db, _, client = env
    db.set_trial(USER, days_from_now=-1)
    assert client.get("/api/user/me").status_code == 200
    body = client.get("/api/subscription").json()
    assert not body["hasAccess"] and body["trial"]["status"] == "EXPIRED"


def test_client_supplied_flags_cannot_grant_access(env):
    db, _, client = env
    db.set_trial(USER, days_from_now=-1)
    res = client.get("/api/reels", headers={"X-Premium": "true"}, params={"isPremium": "true"})
    assert res.status_code == 402


# ── Checkout / verification ──────────────────────────────────────────────────

def test_create_uses_configured_price_in_paise_and_reuses_plan(env):
    db, rz, client = env
    db.set_trial(USER, days_from_now=-1)
    first = client.post("/api/subscription/create").json()
    assert rz.created_plans == [(100, "INR")]  # ₹1 == 100 paise, not 1 and not 10000
    assert first["checkoutUrl"].startswith("https://rzp.io/") and first["keyId"] == "rzp_test_key"
    # Double tap / retry: same checkout, no second Razorpay subscription.
    second = client.post("/api/subscription/create").json()
    assert second["subscriptionId"] == first["subscriptionId"]
    assert rz.created_subs == 1 and len(rz.created_plans) == 1


def test_price_change_creates_a_new_plan(env, monkeypatch):
    db, rz, client = env
    client.post("/api/subscription/create")
    db.subs[USER].update(razorpay_subscription_id=None, status="TRIAL")
    monkeypatch.setattr(settings, "paid_plan_price", 49.5)
    client.post("/api/subscription/create")
    assert rz.created_plans == [(100, "INR"), (4950, "INR")]


def test_live_keys_never_reuse_a_test_mode_plan_or_checkout(env, monkeypatch):
    db, rz, client = env
    db.set_trial(USER, days_from_now=-1)
    test_checkout = client.post("/api/subscription/create").json()["subscriptionId"]
    assert db.subs[USER]["razorpay_mode"] == "test" and len(rz.created_plans) == 1

    # Backend is redeployed with live keys: the test plan and the user's pending test checkout don't exist there.
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_key")
    assert client.get("/api/subscription").json()["status"] == "PENDING"  # no Razorpay lookup of the test sub
    assert client.post("/api/subscription/verify").status_code == 400
    assert client.get("/api/subscription/receipts").json() == {"receipts": []}
    live_checkout = client.post("/api/subscription/create").json()["subscriptionId"]
    assert live_checkout != test_checkout
    assert len(rz.created_plans) == 2  # a new plan was made for live; the cached test plan was not reused
    assert db.subs[USER]["razorpay_subscription_id"] == live_checkout and db.subs[USER]["razorpay_mode"] == "live"


def test_access_is_not_granted_until_backend_verifies_payment(env):
    db, rz, client = env
    db.set_trial(USER, days_from_now=-1)
    sid = client.post("/api/subscription/create").json()["subscriptionId"]

    # User cancelled checkout: Razorpay still says "created".
    body = client.post("/api/subscription/verify").json()
    assert not body["hasAccess"]
    assert client.get("/api/reels").status_code == 402

    rz.activate(sid)
    body = client.post("/api/subscription/verify").json()
    assert body["hasAccess"] and body["accessReason"] == "PAID" and body["plan"] == "PREMIUM"
    assert body["subscription"]["nextBillingDate"] is not None
    assert client.get("/api/reels").status_code == 200


def test_verify_without_started_checkout_is_rejected(env):
    _, _, client = env
    assert client.post("/api/subscription/verify").status_code == 400


def test_already_subscribed_user_cannot_create_a_second_subscription(env):
    db, rz, client = env
    sid = client.post("/api/subscription/create").json()["subscriptionId"]
    rz.activate(sid)
    res = client.post("/api/subscription/create")
    assert res.status_code == 409 and rz.created_subs == 1


def test_verify_ignores_subscription_owned_by_someone_else(env):
    db, rz, client = env
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_x", status="PENDING")
    rz.subscriptions["sub_x"] = {"id": "sub_x", "status": "active", "notes": {"user_id": OTHER},
                                 "current_start": 1, "current_end": 9999999999}
    assert not client.post("/api/subscription/verify").json()["hasAccess"]


# ── Webhooks ─────────────────────────────────────────────────────────────────

def test_webhook_rejects_bad_or_missing_signature(env):
    _, _, client = env
    body = json.dumps({"event": "subscription.activated"}).encode()
    assert client.post("/api/subscription/webhook", content=body).status_code == 400
    assert client.post("/api/subscription/webhook", content=body,
                       headers={"X-Razorpay-Signature": "deadbeef"}).status_code == 400


def test_webhook_lifecycle_and_duplicate_delivery_is_idempotent(env):
    db, rz, client = env
    db.set_trial(USER, days_from_now=-1)
    sid = client.post("/api/subscription/create").json()["subscriptionId"]
    now = int(datetime.now(timezone.utc).timestamp())
    active = {"id": sid, "status": "active", "current_start": now, "current_end": now + 86400 * 30,
              "notes": {"user_id": USER}}

    assert signed_webhook(client, "subscription.activated", active, "evt_a").json()["status"] == "ok"
    assert db.subs[USER]["status"] == "ACTIVE"
    snapshot = dict(db.subs[USER])
    assert signed_webhook(client, "subscription.activated", active, "evt_a").json()["status"] == "duplicate"
    assert db.subs[USER] == snapshot

    failed = {**active, "status": "halted"}
    signed_webhook(client, "subscription.halted", failed, "evt_b")
    assert db.subs[USER]["status"] == "PAYMENT_FAILED"
    assert client.get("/api/reels").status_code == 402

    signed_webhook(client, "subscription.cancelled", {**active, "status": "cancelled"}, "evt_c")
    assert db.subs[USER]["status"] == "CANCELLED"


def test_cancelled_subscription_keeps_access_until_period_end(env):
    db, _, client = env
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    db.set_trial(USER, days_from_now=-30, status="CANCELLED", subscription_start_date=future,
                 subscription_end_date=future)
    assert client.get("/api/reels").status_code == 200
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.subs[USER]["subscription_end_date"] = past
    assert client.get("/api/reels").status_code == 402


def test_stale_subscription_event_does_not_clobber_current_one(env):
    db, _, client = env
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_new", status="PENDING")
    old = {"id": "sub_old", "status": "cancelled", "notes": {"user_id": USER}}
    signed_webhook(client, "subscription.cancelled", old, "evt_old")
    assert db.subs[USER]["status"] == "PENDING" and db.subs[USER]["razorpay_subscription_id"] == "sub_new"


def test_failed_processing_releases_the_event_so_razorpay_retry_is_processed(env, monkeypatch):
    db, _, client = env
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="PENDING")
    boom = {"on": True}
    real_update = subscription_store.update

    def flaky(uid, fields):
        if boom["on"]:
            raise RuntimeError("db down")
        real_update(uid, fields)

    monkeypatch.setattr(subscription_store, "update", flaky)
    entity = {"id": "sub_1", "status": "active", "current_start": 1, "current_end": 9999999999,
              "notes": {"user_id": USER}}
    client_no_raise = TestClient(app, raise_server_exceptions=False)
    assert signed_webhook(client_no_raise, "subscription.activated", entity, "evt_r").status_code == 500
    boom["on"] = False
    assert signed_webhook(client_no_raise, "subscription.activated", entity, "evt_r").json()["status"] == "ok"
    assert db.subs[USER]["status"] == "ACTIVE"


def test_minor_unit_conversion():
    assert razorpay_client.to_minor_units(1) == 100
    assert razorpay_client.to_minor_units(0.5) == 50
    assert razorpay_client.to_minor_units("49.99") == 4999
    assert razorpay_client.to_minor_units(40) == 4000


def test_reel_quota_is_not_enforced_and_reported_as_undecided(env):
    _, _, client = env
    pack = client.get("/api/subscription").json()["reelPack"]
    assert pack == {"price": 40, "currency": "INR", "limit": None}
