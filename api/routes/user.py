from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import account_logic, push, subscription_logic
from api.deps import CurrentUser, get_current_user
from api.errors import ApiError
from api.rate_limit import check_rate_limit
from pipeline.storage import preference_store, push_store

router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/me")
def get_profile(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"user:me:{user.id}", limit=60, window_seconds=60)
    # The JWT's `iat` (issued-at) approximates "last sign in" without a extra
    # round-trip to Supabase's Admin API — good enough given the frontend
    # doesn't currently call this endpoint anywhere.
    last_sign_in = (
        datetime.fromtimestamp(user.issued_at, tz=timezone.utc).isoformat() if user.issued_at else None
    )
    return {"id": user.id, "email": user.email, "lastSignIn": last_sign_in}


# ── Preferences ("Faster Search") ────────────────────────────────────────────

class PreferencesUpdate(BaseModel):
    fasterSearch: bool


def _preferences_dto(user_id: str) -> dict:
    entitled = subscription_logic.entitlements(subscription_logic.ensure_subscription(user_id))["fasterSearch"]
    enabled = preference_store.get_faster_search(user_id)
    # `effective` is what the app should act on: the switch only counts while the plan includes the feature, so a
    # lapsed subscription turns it off by itself and a free user can never end up with it on.
    return {"fasterSearch": {"entitled": entitled, "enabled": enabled, "effective": entitled and enabled}}


@router.get("/preferences")
def get_preferences(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"user:prefs:{user.id}", limit=60, window_seconds=60)
    return _preferences_dto(user.id)


@router.put("/preferences")
def update_preferences(body: PreferencesUpdate, user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"user:prefs:{user.id}", limit=60, window_seconds=60)
    entitled = subscription_logic.entitlements(subscription_logic.ensure_subscription(user.id))["fasterSearch"]
    if body.fasterSearch and not entitled:
        raise ApiError(403, "Faster Search is included with a Premium subscription.", code="PREMIUM_REQUIRED")
    preference_store.set_faster_search(user.id, body.fasterSearch)
    return _preferences_dto(user.id)


# ── Push notification token ──────────────────────────────────────────────────

class PushTokenBody(BaseModel):
    token: str
    platform: str = "android"


@router.put("/push-token")
def register_push_token(body: PushTokenBody, user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"user:push:{user.id}", limit=20, window_seconds=60)
    if not push.is_valid_token(body.token):
        raise ApiError(400, "Invalid push token")
    push_store.upsert(body.token, user.id, body.platform[:20])
    return {"success": True}


@router.delete("/push-token")
def remove_push_token(body: PushTokenBody, user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"user:push:{user.id}", limit=20, window_seconds=60)
    push_store.delete_for_user(user.id, body.token)
    return {"success": True}


# ── Account deletion ─────────────────────────────────────────────────────────

class DeleteAccountBody(BaseModel):
    confirm: bool = False


@router.delete("/me")
def delete_account(body: DeleteAccountBody, user: CurrentUser = Depends(get_current_user)):
    """Permanently deletes the caller's own account. Only the verified session decides whose account that is."""
    check_rate_limit(f"user:delete:{user.id}", limit=5, window_seconds=60)
    if not body.confirm:
        raise ApiError(400, "Account deletion must be confirmed.")
    account_logic.delete_account(user)
    return {"success": True}
