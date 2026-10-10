"""Supabase persistence for Help Center support queries.

Uses the service-role client (server-only), so every query filters by user_id
explicitly, same as supabase_store.py.
"""

from __future__ import annotations

from typing import Any

from pipeline.storage.supabase_store import get_client


def create(user_id: str, user_name: str, user_email: str, subject: str, message: str) -> dict[str, Any]:
    res = (
        get_client()
        .table("support_queries")
        .insert({
            "user_id": user_id,
            "user_name": user_name,
            "user_email": user_email,
            "subject": subject,
            "message": message,
        })
        .execute()
    )
    return res.data[0]


def list_for_user(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    res = (
        get_client()
        .table("support_queries")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []
