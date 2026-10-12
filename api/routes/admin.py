"""Account actions for the admin dashboard (a separate website). Server-to-server only: every call must carry the
shared secret in `X-Admin-Key`. If ADMIN_API_KEY is not set, these endpoints are switched off.

Suspending bans the user in Supabase Auth (their sign-in is refused with `user_banned` and their session cannot be
refreshed; this backend also rejects the session they still hold). Deleting reuses the same routine as a user deleting
their own account, so an active subscription is cancelled first.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from api import account_guard, account_logic
from api.errors import ApiError
from api.rate_limit import check_rate_limit
from api.routes.auth_notice import caller_address
from pipeline.config import settings
from pipeline.storage import account_store, admin_store

logger = logging.getLogger("reelvault.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

_MIN_KEY_LENGTH = 24


def require_admin_key(request: Request, x_admin_key: str | None = Header(default=None)) -> None:
    expected = settings.admin_api_key
    if len(expected) < _MIN_KEY_LENGTH:
        raise ApiError(503, "Admin actions are not configured on this server.")
    check_rate_limit(f"admin:key:{caller_address(request)}", limit=30, window_seconds=60)
    if not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), expected.encode()):
        raise ApiError(401, "Invalid admin key")


class ActionBody(BaseModel):
    actor: str = Field(min_length=3, max_length=254, description="Email of the admin doing this, for the audit log")
    note: str | None = Field(default=None, max_length=300)


def _target(user_id: str, body: ActionBody) -> dict:
    from api.routes.reels import _UUID_RE  # the same id check the rest of the API uses

    if not _UUID_RE.match(user_id):
        raise ApiError(400, "Invalid id")
    user = account_store.get_user(user_id)
    if not user:
        raise ApiError(404, "User not found")
    if user["email"] and user["email"].strip().lower() == body.actor.strip().lower():
        raise ApiError(400, "You cannot do this to your own account.")
    return user


def _audit(body: ActionBody, action: str, user_id: str) -> bool:
    try:
        admin_store.log_action(body.actor.strip().lower(), action, user_id, body.note)
        return True
    except Exception:
        logger.exception("Admin action %s on %s by %s could not be written to the audit log", action, user_id, body.actor)
        return False


@router.post("/users/{user_id}/suspend", dependencies=[Depends(require_admin_key)])
def suspend_user(user_id: str, body: ActionBody):
    user = _target(user_id, body)
    if not user["banned"]:
        account_store.set_suspended(user_id, True)
    account_guard.mark_suspended(user_id)  # takes effect on this server at once
    return {"status": "suspended", "userId": user_id, "audited": _audit(body, "suspend", user_id)}


@router.post("/users/{user_id}/unsuspend", dependencies=[Depends(require_admin_key)])
def unsuspend_user(user_id: str, body: ActionBody):
    user = _target(user_id, body)
    if user["banned"]:
        account_store.set_suspended(user_id, False)
    account_guard.clear(user_id)
    return {"status": "active", "userId": user_id, "audited": _audit(body, "unsuspend", user_id)}


@router.post("/users/{user_id}/delete", dependencies=[Depends(require_admin_key)])
def delete_user(user_id: str, body: ActionBody):
    user = _target(user_id, body)
    account_logic.delete_user(user_id, user["email"], "admin")  # raises 502 (and deletes nothing) if billing can't be stopped
    return {"status": "deleted", "userId": user_id, "audited": _audit(body, "delete", user_id)}
