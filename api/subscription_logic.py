"""Subscription state + access rules. The only place that decides who has access."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from api import razorpay_client
from pipeline.config import settings
from pipeline.storage import subscription_store

logger = logging.getLogger("reelvault.subscription")

# Razorpay subscription.status -> local status. Statuses are Razorpay's documented set.
_RAZORPAY_STATUS_MAP = {
    "created": "PENDING",
    "authenticated": "PENDING",
    "active": "ACTIVE",
    "pending": "PAYMENT_FAILED",  # a charge failed, Razorpay is retrying
    "halted": "PAYMENT_FAILED",   # retries exhausted
    "paused": "PAUSED",
    "cancelled": "CANCELLED",
    "completed": "EXPIRED",
    "expired": "EXPIRED",
}
# Razorpay statuses where the hosted checkout link can still be paid.
PAYABLE_RAZORPAY_STATUSES = {"created", "authenticated"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _from_unix(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None


def ensure_subscription(user_id: str) -> dict[str, Any]:
    """Return the user's subscription row, creating an empty one (no trial yet) on first sight."""
    row = subscription_store.get(user_id)
    if row:
        return row
    subscription_store.create_if_missing(user_id)
    row = subscription_store.get(user_id)
    if not row:
        raise RuntimeError("Could not create a subscription record")
    return row


def start_trial(user_id: str) -> dict[str, Any]:
    """Starts the free trial the first time the user asks for it. Never restarts or extends one."""
    row = ensure_subscription(user_id)
    if row.get("trial_start_date"):
        return row
    start = now_utc()
    subscription_store.start_trial_if_unstarted(user_id, start, start + timedelta(days=settings.trial_duration_days))
    return ensure_subscription(user_id)


def trial_active(row: dict[str, Any], now: datetime) -> bool:
    end = _parse(row.get("trial_end_date"))
    return bool(end and end > now)


def paid_active(row: dict[str, Any], now: datetime) -> bool:
    if row.get("status") == "ACTIVE":
        return True
    # A cancelled subscription keeps working until the period already paid for ends.
    if row.get("status") == "CANCELLED":
        end = _parse(row.get("subscription_end_date"))
        return bool(end and end > now)
    return False


def access_reason(row: dict[str, Any], now: datetime | None = None) -> str:
    now = now or now_utc()
    if paid_active(row, now):
        return "PAID"
    if trial_active(row, now):
        return "TRIAL"
    return "NONE"


def owner_from_notes(entity: dict[str, Any]) -> str | None:
    """The ReelVault user a Razorpay object belongs to, read from its notes, ONLY if it carries our tag.
    Other products on the same Razorpay account may also put a `user_id` in their notes; those must be ignored."""
    notes = entity.get("notes") or {}
    return notes.get("user_id") if notes.get("app") == razorpay_client.APP_TAG else None


def same_mode(row: dict[str, Any]) -> bool:
    """False when the user's stored Razorpay subscription was created under the other key mode
    (e.g. a test-mode checkout left over after switching to live keys): Razorpay can't see it."""
    return row.get("razorpay_mode", "test") == razorpay_client.current_mode()


def apply_razorpay_subscription(user_id: str, rz: dict[str, Any]) -> dict[str, Any]:
    """Write a Razorpay subscription entity's state onto the user's row (idempotent)."""
    row = ensure_subscription(user_id)
    if row.get("razorpay_subscription_id") not in (None, rz["id"]):
        # Event for a superseded subscription (e.g. an old cancelled one) — must not clobber the current one.
        logger.info("Ignoring update for stale Razorpay subscription %s (user %s)", rz["id"], user_id)
        return row

    status = _RAZORPAY_STATUS_MAP.get(rz.get("status", ""), row["status"])
    fields: dict[str, Any] = {"status": status, "razorpay_subscription_id": rz["id"]}
    if rz.get("customer_id"):
        fields["razorpay_customer_id"] = rz["customer_id"]

    period_start = _from_unix(rz.get("current_start"))
    period_end = _from_unix(rz.get("current_end"))
    if status in ("ACTIVE", "CANCELLED", "PAYMENT_FAILED", "PAUSED", "EXPIRED"):
        if period_start and not row.get("subscription_start_date"):
            fields["subscription_start_date"] = _from_unix(rz.get("start_at")) or period_start
        if period_end:
            fields["subscription_end_date"] = period_end
    if status == "ACTIVE":
        fields["plan"] = "PREMIUM"

    subscription_store.update(user_id, fields)
    return {**row, **fields}


def sync_from_razorpay(user_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Pull the authoritative status from Razorpay for the subscription attached to this user."""
    rz_id = row.get("razorpay_subscription_id")
    if not rz_id:
        return row
    rz = razorpay_client.fetch_subscription(rz_id)
    owner = (rz.get("notes") or {}).get("user_id")
    if owner and owner != user_id:
        logger.error("Razorpay subscription %s belongs to %s, not %s", rz_id, owner, user_id)
        return row
    return apply_razorpay_subscription(user_id, rz)


def entitlements(row: dict[str, Any], now: datetime | None = None) -> dict[str, bool]:
    """What the user's plan includes. The single source of truth: the app only mirrors this, and the preference
    endpoint enforces it. Faster Search is a paid-plan feature (a free trial does not include it)."""
    return {"fasterSearch": access_reason(row, now) == "PAID"}


def price_display() -> str:
    amount = settings.paid_plan_price
    symbol = {"INR": "₹"}.get(settings.paid_plan_currency, settings.paid_plan_currency + " ")
    text = f"{amount:g}"
    return f"{symbol}{text}/month"


def to_dto(row: dict[str, Any]) -> dict[str, Any]:
    now = now_utc()
    trial_end = _parse(row.get("trial_end_date"))
    is_trial_active = trial_active(row, now)
    trial_status = "NOT_STARTED" if not row.get("trial_start_date") else ("ACTIVE" if is_trial_active else "EXPIRED")
    days_left = max(0, math.ceil((trial_end - now).total_seconds() / 86400)) if trial_end and is_trial_active else 0
    has_paid_history = bool(row.get("subscription_start_date"))
    reason = access_reason(row, now)

    subscription = None
    if has_paid_history or row.get("status") in ("ACTIVE", "CANCELLED", "PAYMENT_FAILED", "PAUSED", "EXPIRED"):
        subscription = {
            "status": row["status"],
            "startDate": row.get("subscription_start_date"),
            # Only a real, upcoming renewal is a "next billing date".
            "nextBillingDate": row.get("subscription_end_date") if row["status"] == "ACTIVE" else None,
            "endDate": row.get("subscription_end_date"),
        }

    return {
        "hasAccess": reason != "NONE",
        "entitlements": entitlements(row, now),
        "accessReason": reason,
        "status": row["status"],
        "plan": "PREMIUM" if reason == "PAID" or (has_paid_history and reason == "NONE") else "FREE_TRIAL",
        "serverTime": now.isoformat(),
        "trial": {
            "status": trial_status,
            "startDate": row.get("trial_start_date"),
            "endDate": row.get("trial_end_date"),
            "daysRemaining": days_left,
            "durationDays": settings.trial_duration_days,
        },
        "subscription": subscription,
        "pricing": {
            "amount": settings.paid_plan_price,
            "currency": settings.paid_plan_currency,
            "interval": "month",
            "display": price_display(),
        },
        # Future per-reel pack: price is configured, limit is undecided (null = not enforced).
        "reelPack": {
            "price": settings.reel_save_price,
            "currency": settings.paid_plan_currency,
            "limit": settings.reel_save_limit,
        },
        "razorpayKeyId": settings.razorpay_key_id or None,
    }
