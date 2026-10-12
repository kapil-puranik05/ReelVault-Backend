"""Account deletion. Uses REAL signed session tokens (the verification path is the production one); Supabase Auth and
Razorpay are in-memory fakes."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from api import account_guard, razorpay_client
from api.deps import get_current_user
from api.errors import ApiError
from api.main import app
from api.rate_limit import _buckets
from pipeline.config import settings
from pipeline.storage import account_store, admin_store, subscription_store
from tests.test_subscription import OTHER, USER, FakeDb, FakeRazorpay

SECRET = "test-jwt-secret-test-jwt-secret-1234"


def token_for(user_id: str, **claims) -> dict:
    now = int(time.time())
    payload = {"sub": user_id, "aud": "authenticated", "email": "a@b.c", "iat": now, "exp": now + 3600, **claims}
    return {"Authorization": f"Bearer {jwt.encode(payload, SECRET, algorithm='HS256')}"}


class FakeAuth:
    def __init__(self):
        self.users = {USER, OTHER}
        self.fail_delete = 0
        self.outage = False
        self.log: list[str] = []
        self.notices: list[tuple[str, str, str]] = []  # (email, user id, deleted_by)

    def install(self, monkeypatch):
        auth = self

        def get_status(uid):
            if auth.outage:
                raise RuntimeError("supabase down")
            return "ok" if uid in auth.users else "missing"

        def delete_auth_user(uid):
            if auth.fail_delete:
                auth.fail_delete -= 1
                raise RuntimeError("auth delete failed")
            auth.log.append(f"delete:{uid}")
            auth.users.discard(uid)

        monkeypatch.setattr(account_store, "get_status", get_status)
        monkeypatch.setattr(admin_store, "record_deleted", lambda email, uid, by: auth.notices.append((email, uid, by)))
        monkeypatch.setattr(account_store, "delete_auth_user", delete_auth_user)


@pytest.fixture
def acct(monkeypatch):
    _buckets.clear()
    account_guard.reset_for_tests()
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "secret")
    db, rz, auth = FakeDb(), FakeRazorpay(), FakeAuth()
    db.install(monkeypatch)
    rz.install(monkeypatch)
    auth.install(monkeypatch)
    cancelled: list[str] = []

    def cancel(sid):
        auth.log.append(f"cancel:{sid}")
        if rz.cancel_error:
            raise rz.cancel_error
        cancelled.append(sid)
        rz.subscriptions[sid]["status"] = "cancelled"
        return rz.subscriptions[sid]

    rz.cancel_error = None
    monkeypatch.setattr(razorpay_client, "cancel_subscription", cancel)
    app.dependency_overrides.pop(get_current_user, None)  # the real token check runs
    yield db, rz, auth, cancelled, TestClient(app)
    app.dependency_overrides.clear()
    account_guard.reset_for_tests()


def delete(client, headers, body=None):
    return client.request("DELETE", "/api/user/me", headers=headers, json={"confirm": True} if body is None else body)


# ── Authorization ────────────────────────────────────────────────────────────

def test_deletion_requires_a_valid_session(acct):
    _, _, auth, _, client = acct
    assert client.request("DELETE", "/api/user/me", json={"confirm": True}).status_code == 401
    assert client.request("DELETE", "/api/user/me", json={"confirm": True}, headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert auth.users == {USER, OTHER}


def test_token_signed_with_the_wrong_secret_is_refused(acct):
    _, _, auth, _, client = acct
    forged = {"Authorization": "Bearer " + jwt.encode({"sub": USER, "aud": "authenticated", "exp": int(time.time()) + 60}, "wrong-secret-wrong-secret-wrong-12", algorithm="HS256")}
    assert delete(client, forged).status_code == 401 and USER in auth.users


def test_request_must_be_confirmed(acct):
    _, _, auth, _, client = acct
    assert delete(client, token_for(USER), body={}).status_code == 400
    assert delete(client, token_for(USER), body={"confirm": False}).status_code == 400
    assert USER in auth.users  # cancelled / unconfirmed: account untouched


def test_only_the_callers_own_account_is_deleted_whatever_the_request_says(acct):
    _, _, auth, _, client = acct
    res = client.request("DELETE", f"/api/user/me?user_id={OTHER}", headers=token_for(USER),
                         json={"confirm": True, "user_id": OTHER, "userId": OTHER, "id": OTHER})
    assert res.status_code == 200
    assert auth.users == {OTHER}  # the other account survives


# ── Successful deletion ──────────────────────────────────────────────────────

def test_deleted_account_cannot_be_used_again_with_its_still_valid_token(acct):
    _, _, _, _, client = acct
    headers = token_for(USER)
    assert client.get("/api/user/me", headers=headers).status_code == 200
    assert delete(client, headers).json() == {"success": True}
    for method, path in [("get", "/api/user/me"), ("get", "/api/subscription"), ("get", "/api/reels"),
                         ("get", "/api/user/preferences"), ("put", "/api/user/push-token")]:
        res = getattr(client, method)(path, headers=headers, **({"json": {"token": "x"}} if method == "put" else {}))
        assert res.status_code == 401 and res.json()["code"] == "ACCOUNT_DELETED", path


def test_repeating_the_request_after_deletion_is_refused_cleanly(acct):
    _, _, auth, _, client = acct
    headers = token_for(USER)
    assert delete(client, headers).status_code == 200
    again = delete(client, headers)
    assert again.status_code == 401 and again.json()["code"] == "ACCOUNT_DELETED"
    assert auth.log.count(f"delete:{USER}") == 1


def test_account_deleted_elsewhere_is_rejected_after_the_cache_expires(acct):
    _, _, auth, _, client = acct
    headers = token_for(USER)
    assert client.get("/api/user/me", headers=headers).status_code == 200
    auth.users.discard(USER)  # e.g. removed from the Supabase dashboard
    account_guard._state.clear()  # the positive answer was only cached for a few minutes
    assert client.get("/api/user/me", headers=headers).status_code == 401


def test_supabase_outage_does_not_lock_everyone_out(acct):
    _, _, auth, _, client = acct
    auth.outage = True
    assert client.get("/api/user/me", headers=token_for(USER)).status_code == 200


# ── Subscription handling ────────────────────────────────────────────────────

def test_active_subscription_is_cancelled_before_the_account_goes(acct):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="ACTIVE", razorpay_mode="test")
    rz.subscriptions["sub_1"] = {"id": "sub_1", "status": "active", "notes": {"user_id": USER, "app": "reelvault"}}
    assert delete(client, token_for(USER)).status_code == 200
    assert cancelled == ["sub_1"]
    assert auth.log == ["cancel:sub_1", f"delete:{USER}"]  # stop billing first, then delete


def test_failed_cancellation_blocks_deletion_and_a_retry_completes_it(acct):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="ACTIVE", razorpay_mode="test")
    rz.subscriptions["sub_1"] = {"id": "sub_1", "status": "active", "notes": {"user_id": USER}}
    rz.cancel_error = ApiError(502, "provider down")
    res = delete(client, token_for(USER))
    assert res.status_code == 502 and res.json()["code"] == "SUBSCRIPTION_CANCEL_FAILED"
    assert USER in auth.users  # never "deleted but still billed"
    assert client.get("/api/user/me", headers=token_for(USER)).status_code == 200  # still fully usable
    rz.cancel_error = None
    assert delete(client, token_for(USER)).status_code == 200 and USER not in auth.users


@pytest.mark.parametrize("rz_status", ["created", "cancelled", "completed", "expired", "halted", "paused"])
def test_subscriptions_that_cannot_charge_again_are_left_alone(acct, rz_status):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="CANCELLED", razorpay_mode="test")
    rz.subscriptions["sub_1"] = {"id": "sub_1", "status": rz_status, "notes": {"user_id": USER}}
    assert delete(client, token_for(USER)).status_code == 200 and cancelled == []


def test_user_without_any_subscription_just_deletes(acct):
    db, _, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=3)
    assert delete(client, token_for(USER)).status_code == 200 and cancelled == [] and USER not in auth.users


def test_another_accounts_subscription_is_never_cancelled(acct):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_x", status="ACTIVE", razorpay_mode="test")
    rz.subscriptions["sub_x"] = {"id": "sub_x", "status": "active", "notes": {"user_id": OTHER}}
    assert delete(client, token_for(USER)).status_code == 200 and cancelled == []


def test_subscription_from_the_other_key_mode_is_not_touched(acct, monkeypatch):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_t", status="ACTIVE", razorpay_mode="test")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_key")
    assert delete(client, token_for(USER)).status_code == 200 and cancelled == []


def test_subscription_razorpay_no_longer_knows_does_not_block_deletion(acct, monkeypatch):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_gone", status="ACTIVE", razorpay_mode="test")
    monkeypatch.setattr(razorpay_client, "fetch_subscription", lambda sid: (_ for _ in ()).throw(ApiError(400, "not found")))
    assert delete(client, token_for(USER)).status_code == 200 and USER not in auth.users


def test_razorpay_outage_while_checking_stops_deletion_so_it_can_be_retried(acct, monkeypatch):
    db, rz, auth, cancelled, client = acct
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="ACTIVE", razorpay_mode="test")
    monkeypatch.setattr(razorpay_client, "fetch_subscription", lambda sid: (_ for _ in ()).throw(ApiError(502, "down")))
    assert delete(client, token_for(USER)).status_code == 502 and USER in auth.users


# ── Failure handling ─────────────────────────────────────────────────────────

def test_failed_auth_deletion_leaves_a_working_account_and_can_be_retried(acct):
    _, _, auth, _, client = acct
    auth.fail_delete = 1
    failing = TestClient(app, raise_server_exceptions=False)
    assert delete(failing, token_for(USER)).status_code == 500
    assert USER in auth.users and client.get("/api/user/me", headers=token_for(USER)).status_code == 200
    assert delete(client, token_for(USER)).status_code == 200 and USER not in auth.users


def test_deleting_your_own_account_leaves_a_notice_so_signing_in_can_say_so(acct):
    _, _, auth, _, client = acct
    assert delete(client, token_for(USER)).status_code == 200
    assert auth.notices == [("a@b.c", USER, "user")]
