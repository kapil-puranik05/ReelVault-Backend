"""Supabase persistence for per-user preferences (backend-only table; see supabase/user_features.sql)."""

from __future__ import annotations

from datetime import datetime

from pipeline.storage.supabase_store import get_client


def get_faster_search(user_id: str) -> bool:
    """The stored switch. Defaults to on when the user never touched it."""
    res = get_client().table("user_preferences").select("faster_search").eq("user_id", user_id).limit(1).execute()
    return bool(res.data[0]["faster_search"]) if res.data else True


def set_faster_search(user_id: str, enabled: bool) -> None:
    get_client().table("user_preferences").upsert(
        {"user_id": user_id, "faster_search": enabled, "updated_at": datetime.now().astimezone().isoformat()},
        on_conflict="user_id",
    ).execute()
