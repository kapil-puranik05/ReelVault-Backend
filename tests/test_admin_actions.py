"""Admin dashboard actions (suspend / reinstate / delete), the deletion notice shown at sign-in, and the audit log.
Supabase Auth, Razorpay and the notice/audit tables are in-memory fakes; routes, guard and logic are the real code."""

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

KEY = "k" * 32
SECRET = "test-jwt-secret-test-jwt-secret-1234"
HEADERS = {"X-Admin-Key": KEY}
ADMIN = "boss@reelvault.test"
TARGET_EMAIL = "member@example.com"


def body(**extra):
    return {"actor": ADMIN, **extra}


def user_token(user_id=USER):
    now = int(time.time())
    return {"Authorization": "Bearer " + jwt.encode({"sub": user_id, "aud": "authenticated", "email": TARGET_EMAIL, "iat": now, "exp": now + 3600}, SECRET, algorithm="HS256")}


class FakeAccounts:
    """Supabase Auth + the notice/audit tables."""

    def __init__(self):
        self.users = {USER: {"email": TARGET_EMAIL, "banned": False}, OTHER: {"email": "other@example.com", "banned": False}}
        self.tombstones: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.audit_fails = False
        self.log: list[str] = []

    def install(self, monkeypatch):
        a = self

        def get_user(uid):
            u = a.users.get(uid)
            return {"id": uid, **u} if u else None

        def get_status(uid):
            u = a.users.get(uid)
            return "missing" if not u else "suspended" if u["banned"] else "ok"

        def set_suspended(uid, on):
            a.users[uid]["banned"] = on
            a.log.append(f"{'suspend' if on else 'unsuspend'}:{uid}")

        def delete_auth_user(uid):
            a.log.append(f"delete:{uid}")
            a.users.pop(uid, None)

        def email_has_account(email):
            return any(u["email"].lower() == email.lower() for u in a.users.values())

        def log_action(actor, action, target, note=None):
            if a.audit_fails:
                raise RuntimeError("audit table missing")
            a.audit.append({"actor": actor, "action": action, "target": target, "note": note})

        monkeypatch.setattr(account_store, "get_user", get_user)
        monkeypatch.setattr(account_store, "get_status", get_status)
        monkeypatch.setattr(account_store, "set_suspended", set_suspended)
        monkeypatch.setattr(account_store, "delete_auth_user", delete_auth_user)
        monkeypatch.setattr(account_store, "email_has_account", email_has_account)
        monkeypatch.setattr(admin_store, "record_deleted", lambda email, uid, by: a.tombstones.__setitem__(admin_store.hash_email(email), {"user_id": uid, "deleted_by": by}))
        monkeypatch.setattr(admin_store, "deleted_notice", lambda email: a.tombstones.get(admin_store.hash_email(email)))
        monkeypatch.setattr(admin_store, "log_action", log_action)


@pytest.fixture
def adm(monkeypatch):
    _buckets.clear()
    account_guard.reset_for_tests()
    monkeypatch.setattr(settings, "admin_api_key", KEY)
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "secret")
    db, rz, acc = FakeDb(), FakeRazorpay(), FakeAccounts()
    db.install(monkeypatch)
    rz.install(monkeypatch)
    acc.install(monkeypatch)
    rz.cancel_error = None
    cancelled: list[str] = []

    def cancel(sid):
        acc.log.append(f"cancel:{sid}")
        if rz.cancel_error:
            raise rz.cancel_error
        cancelled.append(sid)
        rz.subscriptions[sid]["status"] = "cancelled"
        return rz.subscriptions[sid]

    monkeypatch.setattr(razorpay_client, "cancel_subscription", cancel)
    app.dependency_overrides.pop(get_current_user, None)
    yield db, rz, acc, cancelled, TestClient(app)
    app.dependency_overrides.clear()
    account_guard.reset_for_tests()


def post(client, uid, action, headers=HEADERS, json=None):
    return client.post(f"/api/admin/users/{uid}/{action}", headers=headers, json=body() if json is None else json)


# ── Only the dashboard may call these ────────────────────────────────────────

def test_admin_endpoints_need_the_shared_key(adm):
    _, _, acc, _, client = adm
    for headers in [{}, {"X-Admin-Key": "wrong" * 8}, {"X-Admin-Key": ""}]:
        assert post(client, USER, "delete", headers).status_code == 401
    assert acc.users[USER]["email"] and acc.log == []  # nothing happened


def test_admin_endpoints_are_off_when_no_key_is_configured(adm, monkeypatch):
    _, _, acc, _, client = adm
    for configured in ["", "short"]:
        monkeypatch.setattr(settings, "admin_api_key", configured)
        assert post(client, USER, "delete", {"X-Admin-Key": configured or "x"}).status_code == 503
    assert acc.log == []


def test_a_users_own_session_token_is_not_an_admin_key(adm):
    _, _, acc, _, client = adm
    assert post(client, USER, "delete", {**user_token(), "X-Admin-Key": user_token()["Authorization"]}).status_code == 401
    assert post(client, USER, "suspend", user_token()).status_code == 401 and acc.log == []


def test_guessing_the_key_is_rate_limited(adm):
    _, _, _, _, client = adm
    codes = [post(client, USER, "suspend", {"X-Admin-Key": f"guess-{i}-guess-{i}-guess-{i}-xx"}).status_code for i in range(35)]
    assert codes[0] == 401 and codes[-1] == 429


# ── Suspend / reinstate ──────────────────────────────────────────────────────

def test_suspending_stops_the_users_still_valid_session_at_once_and_reinstating_restores_it(adm):
    _, _, acc, _, client = adm
    me = user_token()
    assert client.get("/api/user/me", headers=me).status_code == 200
    res = post(client, USER, "suspend", json=body(note="spam"))
    assert res.status_code == 200 and res.json()["status"] == "suspended" and acc.users[USER]["banned"]
    refused = client.get("/api/user/me", headers=me)
    assert refused.status_code == 403 and refused.json()["code"] == "ACCOUNT_SUSPENDED"
    assert client.get("/api/subscription", headers=me).status_code == 403
    other = client.get("/api/user/me", headers=user_token(OTHER))
    assert other.status_code == 200  # nobody else is affected

    assert post(client, USER, "unsuspend").json()["status"] == "active"
    assert not acc.users[USER]["banned"] and client.get("/api/user/me", headers=me).status_code == 200


def test_suspending_twice_is_harmless_and_does_not_hit_supabase_again(adm):
    _, _, acc, _, client = adm
    post(client, USER, "suspend"); post(client, USER, "suspend")
    assert acc.log.count(f"suspend:{USER}") == 1
    post(client, USER, "unsuspend"); post(client, USER, "unsuspend")
    assert acc.log.count(f"unsuspend:{USER}") == 1


def test_suspended_status_found_on_supabase_is_enforced_even_if_set_elsewhere(adm):
    _, _, acc, _, client = adm
    acc.users[USER]["banned"] = True  # banned in the Supabase dashboard, not through this backend
    assert client.get("/api/user/me", headers=user_token()).status_code == 403


def test_suspending_does_not_touch_billing_or_data(adm):
    db, rz, acc, cancelled, client = adm
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="ACTIVE", razorpay_mode="test")
    rz.subscriptions["sub_1"] = {"id": "sub_1", "status": "active", "notes": {"user_id": USER}}
    post(client, USER, "suspend")
    assert cancelled == [] and USER in db.subs and USER in acc.users


# ── Delete ───────────────────────────────────────────────────────────────────

def test_admin_delete_cancels_billing_removes_the_account_leaves_a_notice_and_blocks_the_old_token(adm):
    db, rz, acc, cancelled, client = adm
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="ACTIVE", razorpay_mode="test")
    rz.subscriptions["sub_1"] = {"id": "sub_1", "status": "active", "notes": {"user_id": USER}}
    me = user_token()
    res = post(client, USER, "delete", json=body(note="abuse report #12"))
    assert res.status_code == 200 and res.json()["status"] == "deleted"
    assert acc.log == ["cancel:sub_1", f"delete:{USER}"]          # billing stopped first
    assert USER not in acc.users and OTHER in acc.users
    assert acc.tombstones[admin_store.hash_email(TARGET_EMAIL)] == {"user_id": USER, "deleted_by": "admin"}
    gone = client.get("/api/user/me", headers=me)
    assert gone.status_code == 401 and gone.json()["code"] == "ACCOUNT_DELETED"


def test_failed_cancellation_leaves_the_account_untouched(adm):
    db, rz, acc, _, client = adm
    db.set_trial(USER, days_from_now=-1, razorpay_subscription_id="sub_1", status="ACTIVE", razorpay_mode="test")
    rz.subscriptions["sub_1"] = {"id": "sub_1", "status": "active", "notes": {"user_id": USER}}
    rz.cancel_error = ApiError(502, "provider down")
    res = post(client, USER, "delete")
    assert res.status_code == 502 and res.json()["code"] == "SUBSCRIPTION_CANCEL_FAILED"
    assert USER in acc.users and acc.tombstones == {} and acc.audit == []
    assert client.get("/api/user/me", headers=user_token()).status_code == 200


def test_unknown_or_malformed_targets_are_refused(adm):
    _, _, acc, _, client = adm
    assert post(client, "33333333-3333-3333-3333-333333333333", "delete").status_code == 404
    assert post(client, "not-a-uuid", "delete").status_code == 400
    assert post(client, USER, "delete", json={}).status_code == 400
    assert post(client, USER, "delete", json={"actor": "x"}).status_code == 400
    assert acc.log == []


def test_an_admin_cannot_act_on_their_own_account(adm):
    _, _, acc, _, client = adm
    assert post(client, USER, "delete", json={"actor": TARGET_EMAIL.upper()}).status_code == 400
    assert post(client, USER, "suspend", json={"actor": TARGET_EMAIL}).status_code == 400
    assert acc.log == [] and USER in acc.users


# ── Audit log ────────────────────────────────────────────────────────────────

def test_every_action_is_logged_with_who_what_and_whom(adm):
    _, _, acc, _, client = adm
    post(client, USER, "suspend", json=body(note="spam"))
    post(client, USER, "unsuspend")
    post(client, USER, "delete", json=body(note="confirmed abuse"))
    assert acc.audit == [
        {"actor": ADMIN, "action": "suspend", "target": USER, "note": "spam"},
        {"actor": ADMIN, "action": "unsuspend", "target": USER, "note": None},
        {"actor": ADMIN, "action": "delete", "target": USER, "note": "confirmed abuse"},
    ]


def test_a_broken_audit_log_does_not_undo_or_block_the_action_but_is_reported(adm):
    _, _, acc, _, client = adm
    acc.audit_fails = True
    res = post(client, USER, "suspend")
    assert res.status_code == 200 and res.json()["audited"] is False and acc.users[USER]["banned"]


# ── "Was this account deleted?" at sign-in ───────────────────────────────────

def notice(client, email):
    return client.post("/api/auth/account-notice", json={"email": email})


def test_a_deleted_account_gets_a_deletion_notice_at_sign_in(adm):
    _, _, acc, _, client = adm
    post(client, USER, "delete")
    assert notice(client, TARGET_EMAIL).json() == {"status": "deleted", "deletedBy": "admin"}
    assert notice(client, "  Member@Example.COM ").json()["status"] == "deleted"  # same address, however typed


def test_everyone_else_gets_a_neutral_answer(adm):
    _, _, acc, _, client = adm
    post(client, USER, "delete")
    assert notice(client, "never-existed@example.com").json() == {"status": "none"}
    assert notice(client, "other@example.com").json() == {"status": "none"}  # a live account is not "deleted"


def test_someone_who_signed_up_again_with_the_same_email_is_not_told_they_were_deleted(adm):
    _, _, acc, _, client = adm
    post(client, USER, "delete")
    acc.users["44444444-4444-4444-4444-444444444444"] = {"email": TARGET_EMAIL, "banned": False}
    assert notice(client, TARGET_EMAIL).json() == {"status": "none"}


def test_the_notice_keeps_no_readable_email(adm):
    _, _, acc, _, client = adm
    post(client, USER, "delete")
    stored = repr(acc.tombstones)
    assert TARGET_EMAIL not in stored and "member" not in stored.lower()
    assert admin_store.hash_email(TARGET_EMAIL) == admin_store.hash_email(" MEMBER@example.com ")  # stable, case-insensitive
    assert admin_store.hash_email(TARGET_EMAIL) != admin_store.hash_email("other@example.com")


def test_the_notice_hash_depends_on_a_server_secret(adm, monkeypatch):
    first = admin_store.hash_email(TARGET_EMAIL)
    monkeypatch.setattr(settings, "supabase_jwt_secret", "a-different-secret-a-different-secret!")
    assert admin_store.hash_email(TARGET_EMAIL) != first  # a copy of the table can't be checked against guessed emails


def test_the_notice_question_is_rate_limited_and_validates_input(adm):
    _, _, _, _, client = adm
    assert notice(client, "not an email").status_code == 400
    assert notice(client, "x" * 400 + "@example.com").status_code == 400
    codes = [notice(client, f"probe{i}@example.com").status_code for i in range(15)]
    assert codes[:1] == [200] and codes[-1] == 429
