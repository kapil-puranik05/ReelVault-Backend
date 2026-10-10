"""Supabase persistence for subscriptions, Razorpay plans and webhook events.

Uses the service-role client (server-only), so every query filters by user_id
explicitly, same as supabase_store.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.storage.supabase_store import get_client


def get(user_id: str) -> dict[str, Any] | None:
    res = get_client().table("subscriptions").select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_by_razorpay_id(razorpay_subscription_id: str) -> dict[str, Any] | None:
    res = (
        get_client()
        .table("subscriptions")
        .select("*")
        .eq("razorpay_subscription_id", razorpay_subscription_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def create_if_missing(user_id: str) -> None:
    """Idempotent. New rows have no trial dates and status NEW: the user hasn't chosen a plan yet."""
    get_client().table("subscriptions").upsert(
        {"user_id": user_id}, on_conflict="user_id", ignore_duplicates=True
    ).execute()


def start_trial_if_unstarted(user_id: str, start: datetime, end: datetime) -> bool:
    """Compare-and-set on trial_start_date IS NULL: a trial starts at most once per account,
    even under double taps or concurrent requests. Returns whether this call started it."""
    client = get_client()
    res = (
        client.table("subscriptions")
        .update({
            "trial_start_date": start.isoformat(),
            "trial_end_date": end.isoformat(),
            "updated_at": start.isoformat(),
        })
        .eq("user_id", user_id)
        .is_("trial_start_date", "null")
        .execute()
    )
    if not res.data:
        return False
    client.table("subscriptions").update({"status": "TRIAL"}).eq("user_id", user_id).eq("status", "NEW").execute()
    return True


def update(user_id: str, fields: dict[str, Any]) -> None:
    fields = {**fields, "updated_at": datetime.now().astimezone().isoformat()}
    get_client().table("subscriptions").update(fields).eq("user_id", user_id).execute()


def claim_razorpay_subscription(user_id: str, new_id: str, expected_current: str | None, mode: str) -> bool:
    """Compare-and-set of razorpay_subscription_id, so two concurrent 'Subscribe'
    taps can't both attach a different subscription to the same user."""
    query = (
        get_client()
        .table("subscriptions")
        .update({"razorpay_subscription_id": new_id, "status": "PENDING", "razorpay_mode": mode})
        .eq("user_id", user_id)
    )
    query = query.is_("razorpay_subscription_id", "null") if expected_current is None else query.eq(
        "razorpay_subscription_id", expected_current
    )
    return len(query.execute().data) > 0


def get_plan_id(amount_minor: int, currency: str, mode: str, period: str = "monthly") -> str | None:
    res = (
        get_client()
        .table("billing_plans")
        .select("id")
        .eq("amount_minor", amount_minor)
        .eq("currency", currency)
        .eq("period", period)
        .eq("mode", mode)
        .limit(1)
        .execute()
    )
    return res.data[0]["id"] if res.data else None


def save_plan(plan_id: str, amount_minor: int, currency: str, mode: str, period: str = "monthly") -> None:
    get_client().table("billing_plans").upsert(
        {"id": plan_id, "amount_minor": amount_minor, "currency": currency, "period": period, "mode": mode},
        on_conflict="id",
        ignore_duplicates=True,
    ).execute()


def record_event(event_id: str, event: str) -> bool:
    """True if this event id is new; False if it was already recorded (duplicate delivery)."""
    res = (
        get_client()
        .table("razorpay_events")
        .upsert({"id": event_id, "event": event}, on_conflict="id", ignore_duplicates=True)
        .execute()
    )
    return len(res.data) > 0


def forget_event(event_id: str) -> None:
    get_client().table("razorpay_events").delete().eq("id", event_id).execute()
