"""Supabase persistence for the payments ledger (backend-only table; see supabase/payments.sql).

Uses the service-role client (server-only), so every query filters explicitly, same as
subscription_store.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.storage.supabase_store import get_client


def get(payment_id: str) -> dict[str, Any] | None:
    res = get_client().table("payments").select("*").eq("id", payment_id).limit(1).execute()
    return res.data[0] if res.data else None


def upsert(row: dict[str, Any]) -> None:
    """Insert or update by Razorpay payment id, so redelivered webhooks never duplicate a payment."""
    row = {**row, "updated_at": datetime.now().astimezone().isoformat()}
    get_client().table("payments").upsert(row, on_conflict="id").execute()
