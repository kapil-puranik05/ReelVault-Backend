"""Supabase persistence for Expo push tokens (backend-only table; see supabase/user_features.sql)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.storage.supabase_store import get_client


def upsert(token: str, user_id: str, platform: str) -> None:
    """One row per token. A token that moves to another account (shared phone, re-login) is reassigned, so the
    previous account stops receiving this device's notifications."""
    get_client().table("push_tokens").upsert(
        {"token": token, "user_id": user_id, "platform": platform, "updated_at": datetime.now().astimezone().isoformat()},
        on_conflict="token",
    ).execute()


def delete_for_user(user_id: str, token: str) -> None:
    get_client().table("push_tokens").delete().eq("user_id", user_id).eq("token", token).execute()


def delete_token(token: str) -> None:
    get_client().table("push_tokens").delete().eq("token", token).execute()


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    return get_client().table("push_tokens").select("*").eq("user_id", user_id).execute().data or []
