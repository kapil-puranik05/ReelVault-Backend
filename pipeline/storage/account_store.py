"""Supabase Auth operations behind account deletion. Service-role only (never exposed to the app)."""

from __future__ import annotations

from supabase_auth.errors import AuthApiError

from pipeline.storage.supabase_store import get_client


def _is_not_found(exc: AuthApiError) -> bool:
    return exc.status == 404 or getattr(exc, "code", None) == "user_not_found"


def user_exists(user_id: str) -> bool:
    try:
        res = get_client().auth.admin.get_user_by_id(user_id)
    except AuthApiError as exc:
        if _is_not_found(exc):
            return False
        raise
    return bool(res and res.user)


def delete_auth_user(user_id: str) -> None:
    """Removes the Supabase Auth user. Every per-user table references auth.users with ON DELETE CASCADE (reels,
    frames, subscription, jobs, support queries, preferences, push tokens); payments keep their rows with the user
    link cleared. Already-gone is success, so a retried request is harmless."""
    try:
        get_client().auth.admin.delete_user(user_id)
    except AuthApiError as exc:
        if not _is_not_found(exc):
            raise
