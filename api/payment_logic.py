"""Records Razorpay payments in our own `payments` table (backend-only ledger).

Fed exclusively by signature-verified webhooks. Stores amounts, status and Razorpay ids — never card,
UPI or bank details, customer email or phone. Idempotent: the Razorpay payment id is the primary key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from api import razorpay_client, subscription_logic
from api.errors import ApiError
from pipeline.storage import payment_store, subscription_store

logger = logging.getLogger("reelvault.payments")

# Events whose payload carries a payment entity. `subscription.charged` is how a renewal arrives.
PAYMENT_EVENTS = {"payment.captured", "payment.failed", "refund.processed"}
SUBSCRIPTION_EVENTS_WITH_PAYMENT = {"subscription.charged"}

# A payment's status only moves forward; a late `payment.authorized` must not undo `captured`.
_STATUS_RANK = {"created": 0, "authorized": 1, "failed": 1, "captured": 2, "refunded": 3}


def _resolve_user_id(payment: dict[str, Any], subscription: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(user_id, razorpay_subscription_id) for a payment, or (None, ...) when it isn't one of ours."""
    sub_id = (subscription or {}).get("id")
    if not sub_id and payment.get("invoice_id"):
        try:
            sub_id = razorpay_client.fetch_invoice(payment["invoice_id"]).get("subscription_id")
        except ApiError as exc:
            if exc.status_code != 400:  # 400 = Razorpay says it doesn't know the invoice; else let the webhook retry
                raise
    if not sub_id:
        return None, None
    row = subscription_store.get_by_razorpay_id(sub_id)
    if row:
        return row["user_id"], sub_id
    return (subscription_logic.owner_from_notes(subscription) if subscription else None), sub_id


def _to_iso(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch else None


def record_payment(payment: dict[str, Any], subscription: dict[str, Any] | None = None) -> bool:
    """Upsert one Razorpay payment entity. Returns False (and stores nothing) if it isn't tied to a
    ReelVault user — e.g. a payment from another product sharing the same Razorpay account."""
    payment_id = payment.get("id")
    if not payment_id:
        return False

    existing = payment_store.get(payment_id)
    if existing and existing.get("user_id"):
        user_id, sub_id = existing["user_id"], existing.get("razorpay_subscription_id")  # no Razorpay lookup needed
    else:
        user_id, sub_id = _resolve_user_id(payment, subscription)
    if not user_id:
        logger.warning("Ignoring payment %s: not linked to a ReelVault subscription", payment_id)
        return False

    status = payment.get("status") or "created"
    refunded = int(payment.get("amount_refunded") or 0)
    if existing:
        refunded = max(refunded, int(existing.get("amount_refunded_minor") or 0))
        if _STATUS_RANK.get(existing["status"], 0) > _STATUS_RANK.get(status, 0):
            status = existing["status"]  # keep the furthest state reached

    payment_store.upsert({
        "id": payment_id,
        "user_id": user_id,
        "razorpay_subscription_id": sub_id,
        "razorpay_order_id": payment.get("order_id"),
        "razorpay_invoice_id": payment.get("invoice_id"),
        "amount_minor": int(payment.get("amount") or 0),
        "currency": payment.get("currency") or "INR",
        "status": status,
        "method": payment.get("method"),
        "fee_minor": payment.get("fee"),
        "tax_minor": payment.get("tax"),
        "amount_refunded_minor": refunded,
        "error_code": payment.get("error_code"),
        "error_description": payment.get("error_description"),
        "livemode": razorpay_client.current_mode() == "live",
        "razorpay_created_at": _to_iso(payment.get("created_at")),
    })
    return True
