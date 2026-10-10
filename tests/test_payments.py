"""Payments ledger: Razorpay webhooks -> `payments` table. Same in-memory fakes as test_subscription.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import razorpay_client
from api.errors import ApiError
from api.main import app
from pipeline.config import settings
from tests.test_subscription import OTHER, USER, env, signed_webhook  # noqa: F401  (env is a fixture)

SUB = {"id": "sub_1", "status": "active", "current_start": 1, "current_end": 9999999999, "notes": {"user_id": USER}}


def pay(**over):
    return {"id": "pay_1", "amount": 100, "currency": "INR", "status": "captured", "method": "upi",
            "order_id": "order_1", "invoice_id": "inv_1", "fee": 2, "tax": 0, "amount_refunded": 0,
            "created_at": 1_800_000_000, "email": "a@b.c", "contact": "+911234567890",
            "card": {"last4": "1111"}, **over}


@pytest.fixture
def ready(env):
    db, rz, client = env
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="PENDING")
    return db, rz, client


def test_renewal_records_payment_for_the_right_user_without_sensitive_data(ready):
    db, _, client = ready
    res = signed_webhook(client, "subscription.charged", SUB, "evt_1", payment=pay())
    assert res.json()["status"] == "ok"
    row = db.payments["pay_1"]
    assert row["user_id"] == USER and row["razorpay_subscription_id"] == "sub_1"
    assert row["amount_minor"] == 100 and row["currency"] == "INR" and row["status"] == "captured"
    assert row["fee_minor"] == 2 and row["method"] == "upi" and row["livemode"] is False
    assert row["razorpay_created_at"].startswith("2027-")
    assert not {"email", "contact", "card"} & set(row)  # no customer or instrument details stored
    assert db.subs[USER]["status"] == "ACTIVE"  # the subscription state is still applied as before


def test_redelivery_and_double_events_keep_one_row(ready):
    db, _, client = ready
    signed_webhook(client, "subscription.charged", SUB, "evt_1", payment=pay())
    assert signed_webhook(client, "subscription.charged", SUB, "evt_1", payment=pay()).json()["status"] == "duplicate"
    signed_webhook(client, "payment.captured", None, "evt_2", payment=pay())  # same payment, different event
    assert list(db.payments) == ["pay_1"]


def test_status_never_moves_backwards_and_refunds_only_grow(ready):
    db, _, client = ready
    signed_webhook(client, "subscription.charged", SUB, "evt_1", payment=pay(amount_refunded=40))
    signed_webhook(client, "payment.captured", None, "evt_2", payment=pay(status="authorized", amount_refunded=0))
    row = db.payments["pay_1"]
    assert row["status"] == "captured" and row["amount_refunded_minor"] == 40
    signed_webhook(client, "payment.captured", None, "evt_3", payment=pay(status="refunded", amount_refunded=100))
    assert db.payments["pay_1"]["status"] == "refunded" and db.payments["pay_1"]["amount_refunded_minor"] == 100


def test_payment_event_alone_finds_its_user_through_the_invoice(ready):
    db, rz, client = ready
    rz.invoices["inv_1"] = {"id": "inv_1", "subscription_id": "sub_1"}
    failed = pay(id="pay_f", status="failed", error_code="BAD_REQUEST_ERROR", error_description="Card declined")
    assert signed_webhook(client, "payment.failed", None, "evt_f", payment=failed).json()["status"] == "ok"
    row = db.payments["pay_f"]
    assert row["user_id"] == USER and row["status"] == "failed" and row["error_description"] == "Card declined"


def test_refund_event_without_payment_entity_reads_the_payment(ready):
    db, rz, client = ready
    signed_webhook(client, "subscription.charged", SUB, "evt_1", payment=pay())
    rz.payments["pay_1"] = pay(status="refunded", amount_refunded=100)
    signed_webhook(client, "refund.processed", None, "evt_r", refund={"id": "rfnd_1", "payment_id": "pay_1", "amount": 100})
    assert db.payments["pay_1"]["status"] == "refunded" and db.payments["pay_1"]["amount_refunded_minor"] == 100


def test_payment_not_linked_to_a_reelvault_user_is_not_stored(ready):
    db, rz, client = ready
    rz.invoices["inv_x"] = {"id": "inv_x", "subscription_id": "sub_unknown"}
    res = signed_webhook(client, "payment.captured", None, "evt_x", payment=pay(id="pay_x", invoice_id="inv_x"))
    assert res.status_code == 200 and "pay_x" not in db.payments
    # Payment with no invoice at all (a one-off from another product on the same Razorpay account)
    signed_webhook(client, "payment.captured", None, "evt_y", payment=pay(id="pay_y", invoice_id=None))
    assert "pay_y" not in db.payments


def test_invoice_lookup_failure_releases_event_for_razorpay_retry(ready, monkeypatch):
    db, rz, client = ready
    calls = {"n": 0}

    def flaky(_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ApiError(502, "provider down")
        return {"id": "inv_1", "subscription_id": "sub_1"}

    monkeypatch.setattr(razorpay_client, "fetch_invoice", flaky)
    c = TestClient(app, raise_server_exceptions=False)
    assert signed_webhook(c, "payment.captured", None, "evt_z", payment=pay(id="pay_z")).status_code == 502  # non-2xx: Razorpay retries
    assert "pay_z" not in db.payments
    assert signed_webhook(c, "payment.captured", None, "evt_z", payment=pay(id="pay_z")).json()["status"] == "ok"
    assert db.payments["pay_z"]["user_id"] == USER


def test_livemode_flag_follows_the_key_in_use(ready, monkeypatch):
    db, _, client = ready
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc")
    signed_webhook(client, "subscription.charged", SUB, "evt_1", payment=pay())
    assert db.payments["pay_1"]["livemode"] is True



# ── Shared Razorpay account: ReelVault must ignore other products' objects ─────

FOREIGN_USER = "99999999-9999-9999-9999-999999999999"


def test_other_products_subscription_is_ignored_even_if_its_notes_carry_a_user_id(ready):
    db, _, client = ready
    foreign = {"id": "sub_nosher", "status": "active", "current_start": 1, "current_end": 9999999999,
               "notes": {"user_id": FOREIGN_USER}}  # untagged: not created by ReelVault
    res = signed_webhook(client, "subscription.charged", foreign, "evt_n", payment=pay(id="pay_nosher"))
    assert res.status_code == 200 and res.json()["status"] == "unknown_subscription"
    assert FOREIGN_USER not in db.subs and "pay_nosher" not in db.payments
    assert db.subs[USER]["status"] == "PENDING"  # ReelVault's own user untouched


def test_other_products_standalone_payment_and_refund_events_are_ignored(ready):
    db, rz, client = ready
    rz.invoices["inv_n"] = {"id": "inv_n", "subscription_id": "sub_nosher"}
    rz.payments["pay_n"] = pay(id="pay_n", invoice_id="inv_n")
    for ev, kw in [("payment.captured", {"payment": pay(id="pay_n", invoice_id="inv_n")}),
                   ("payment.failed", {"payment": pay(id="pay_n2", status="failed", invoice_id="inv_n")}),
                   ("refund.processed", {"refund": {"id": "rfnd_n", "payment_id": "pay_n", "amount": 100}})]:
        assert signed_webhook(client, ev, None, f"evt_{ev}", **kw).status_code == 200
    assert db.payments == {} and list(db.subs) == [USER]


def test_reelvault_tagged_subscription_unknown_to_db_is_still_applied(env):
    """Webhook that beats our own DB write (create -> claim race): the tag lets it through."""
    db, _, client = env
    db.set_trial(USER, days_from_now=-1)  # no razorpay_subscription_id saved yet
    ours = {"id": "sub_race", "status": "active", "current_start": 1, "current_end": 9999999999,
            "notes": {"user_id": USER, "app": "reelvault"}}
    assert signed_webhook(client, "subscription.activated", ours, "evt_race").json()["status"] == "ok"
    assert db.subs[USER]["status"] == "ACTIVE" and db.subs[USER]["razorpay_subscription_id"] == "sub_race"
