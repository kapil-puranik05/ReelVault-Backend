"""Supabase Auth operations behind account deletion and suspension. Service-role only (never exposed to the app)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from supabase_auth.errors import AuthApiError

from pipeline.config import settings
from pipeline.storage.supabase_store import get_client

SUSPEND_FOR = "876000h"  # about a hundred years: Supabase has no "forever", and "none" lifts it


def _is_not_found(exc: AuthApiError) -> bool:
    return exc.status == 404 or getattr(exc, "code", None) == "user_not_found"


def _banned(banned_until: Any) -> bool:
    if not banned_until:
        return False
    try:
        until = banned_until if isinstance(banned_until, datetime) else datetime.fromisoformat(str(banned_until).replace("Z", "+00:00"))
    except ValueError:
        return False
    return until > datetime.now(timezone.utc)


def get_user(user_id: str) -> dict[str, Any] | None:
    """{"id", "email", "banned"} for an existing account, or None if it does not exist."""
    try:
        res = get_client().auth.admin.get_user_by_id(user_id)
    except AuthApiError as exc:
        if _is_not_found(exc):
            return None
        raise
    user = res.user if res else None
    if not user:
        return None
    return {"id": user.id, "email": user.email or "", "banned": _banned(getattr(user, "banned_until", None))}


def get_status(user_id: str) -> str:
    """'ok' | 'suspended' | 'missing' (deleted)."""
    user = get_user(user_id)
    if not user:
        return "missing"
    return "suspended" if user["banned"] else "ok"


def set_suspended(user_id: str, suspended: bool) -> None:
    """Supabase then refuses the user's sign-in (code `user_banned`) and refuses to refresh their session."""
    get_client().auth.admin.update_user_by_id(user_id, {"ban_duration": SUSPEND_FOR if suspended else "none"})


def email_has_account(email: str) -> bool:
    """Whether an account with exactly this email exists right now (used so a re-registered email is not called 'deleted')."""
    res = httpx.get(
        f"{settings.supabase_url}/auth/v1/admin/users",
        params={"filter": email.strip().lower(), "per_page": 50},  # Supabase stores emails lower-cased and its filter is case-sensitive
        headers={"apikey": settings.supabase_service_role_key, "Authorization": f"Bearer {settings.supabase_service_role_key}"},
        timeout=15.0,
    )
    res.raise_for_status()
    return any((u.get("email") or "").lower() == email.lower() for u in res.json().get("users", []))


def delete_auth_user(user_id: str) -> None:
    """Removes the Supabase Auth user. Every per-user table references auth.users with ON DELETE CASCADE (reels,
    frames, subscription, jobs, support queries, preferences, push tokens); payments keep their rows with the user
    link cleared. Already-gone is success, so a retried request is harmless."""
    try:
        get_client().auth.admin.delete_user(user_id)
    except AuthApiError as exc:
        if not _is_not_found(exc):
            raise
