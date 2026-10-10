from __future__ import annotations

import logging
import threading
from collections import defaultdict

from fastapi import APIRouter, Depends, Request

from api import payment_logic, razorpay_client, subscription_logic
from api.deps import CurrentUser, get_current_user
from api.errors import ApiError
from api.rate_limit import check_rate_limit
from pipeline.config import settings
from pipeline.storage import subscription_store

logger = logging.getLogger("reelvault.subscription")

router = APIRouter(prefix="/api/subscription", tags=["subscription"])

# Serializes 'create' per user within this process (double-tap / client retry);
# the compare-and-set in subscription_store covers anything that slips past.
_create_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def _ensure_plan_id() -> str:
    amount_minor = razorpay_client.to_minor_units(settings.paid_plan_price)
    currency = settings.paid_plan_currency
    mode = razorpay_client.current_mode()
    plan_id = subscription_store.get_plan_id(amount_minor, currency, mode)
    if plan_id:
        return plan_id
    plan = razorpay_client.create_plan(amount_minor, currency)
    subscription_store.save_plan(plan["id"], amount_minor, currency, mode)
    return plan["id"]


@router.get("")
def get_subscription(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"sub:get:{user.id}", limit=60, window_seconds=60)
    row = subscription_logic.ensure_subscription(user.id)

    # Self-heal if a webhook was missed/delayed: a pending checkout may have been paid.
    if row.get("razorpay_subscription_id") and row["status"] == "PENDING" and subscription_logic.same_mode(row):
        try:
            row = subscription_logic.sync_from_razorpay(user.id, row)
        except ApiError:
            pass  # provider hiccup — serve what we have

    return subscription_logic.to_dto(row)


@router.post("/start-trial")
def start_trial(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"sub:trial:{user.id}", limit=10, window_seconds=60)
    return subscription_logic.to_dto(subscription_logic.start_trial(user.id))


@router.post("/create")
def create_subscription(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"sub:create:{user.id}", limit=10, window_seconds=60)

    with _create_locks[user.id]:
        row = subscription_logic.ensure_subscription(user.id)
        existing_id = row.get("razorpay_subscription_id")

        # A checkout made under the other key mode (test vs live) doesn't exist at Razorpay now:
        # skip it, and the claim below replaces it with a fresh one.
        if existing_id and subscription_logic.same_mode(row):
            rz = razorpay_client.fetch_subscription(existing_id)
            row = subscription_logic.apply_razorpay_subscription(user.id, rz)
            if row["status"] == "ACTIVE":
                raise ApiError(409, "You already have an active subscription.", code="ALREADY_SUBSCRIBED")
            if rz.get("status") in subscription_logic.PAYABLE_RAZORPAY_STATUSES and rz.get("short_url"):
                # Unfinished checkout: hand back the same one rather than creating a duplicate.
                return _checkout_payload(rz)

        plan_id = _ensure_plan_id()
        rz = razorpay_client.create_subscription(plan_id, settings.paid_plan_total_count, user.id)
        if not subscription_store.claim_razorpay_subscription(user.id, rz["id"], existing_id, razorpay_client.current_mode()):
            raise ApiError(409, "A subscription is already being set up. Please try again.")
        return _checkout_payload(rz)


def _checkout_payload(rz: dict) -> dict:
    return {
        "subscriptionId": rz["id"],
        "checkoutUrl": rz["short_url"],
        "keyId": settings.razorpay_key_id,
        "amount": settings.paid_plan_price,
        "currency": settings.paid_plan_currency,
    }


@router.post("/verify")
def verify_subscription(user: CurrentUser = Depends(get_current_user)):
    """Re-reads the subscription's status from Razorpay itself. The client supplies
    nothing that could grant access — the subscription id comes from our own DB."""
    check_rate_limit(f"sub:verify:{user.id}", limit=30, window_seconds=60)
    row = subscription_logic.ensure_subscription(user.id)
    if not row.get("razorpay_subscription_id") or not subscription_logic.same_mode(row):
        raise ApiError(400, "No subscription checkout has been started.")
    row = subscription_logic.sync_from_razorpay(user.id, row)
    return subscription_logic.to_dto(row)


@router.get("/receipts")
def list_receipts(user: CurrentUser = Depends(get_current_user)):
    """The caller's own payment receipts, read live from Razorpay. The subscription id comes
    from our DB row for the authenticated user — never from the request — so one user can't
    request another's receipts. Nothing is stored or fabricated here."""
    check_rate_limit(f"sub:receipts:{user.id}", limit=30, window_seconds=60)
    row = subscription_logic.ensure_subscription(user.id)
    rz_id = row.get("razorpay_subscription_id")
    if not rz_id or not subscription_logic.same_mode(row):
        return {"receipts": []}

    receipts = []
    for inv in razorpay_client.list_invoices(rz_id):
        if (inv.get("notes") or {}).get("user_id", user.id) != user.id:
            continue
        # Only charges that actually went through are receipts.
        if inv.get("status") != "paid":
            continue
        receipts.append({
            "id": inv["id"],
            "amount": (inv.get("amount_paid") or inv.get("amount") or 0) / 100,
            "currency": inv.get("currency", settings.paid_plan_currency),
            "status": inv["status"],
            "paidAt": subscription_logic._from_unix(inv.get("paid_at")),
            "description": inv.get("description") or "ReelVault Premium",
            "receiptUrl": inv.get("short_url"),
        })
    receipts.sort(key=lambda r: r["paidAt"] or "", reverse=True)
    return {"receipts": receipts}


_SUBSCRIPTION_EVENTS = {
    "subscription.authenticated",
    "subscription.activated",
    "subscription.charged",
    "subscription.completed",
    "subscription.updated",
    "subscription.pending",
    "subscription.halted",
    "subscription.cancelled",
    "subscription.paused",
    "subscription.resumed",
}


@router.post("/webhook")
async def razorpay_webhook(request: Request):
    raw_body = await request.body()
    if not razorpay_client.verify_webhook_signature(raw_body, request.headers.get("X-Razorpay-Signature")):
        raise ApiError(400, "Invalid webhook signature")

    try:
        payload = await request.json()
    except ValueError:
        raise ApiError(400, "Invalid webhook body")

    event = payload.get("event", "")
    event_id = request.headers.get("X-Razorpay-Event-Id")
    is_subscription_event = event in _SUBSCRIPTION_EVENTS
    if not is_subscription_event and event not in payment_logic.PAYMENT_EVENTS:
        return {"status": "ignored"}

    body = payload.get("payload") or {}
    entity = (body.get("subscription") or {}).get("entity") or {}
    payment = (body.get("payment") or {}).get("entity") or {}
    refund = (body.get("refund") or {}).get("entity") or {}
    if is_subscription_event and not entity.get("id"):
        raise ApiError(400, "Webhook has no subscription")
    if not is_subscription_event and not (payment.get("id") or refund.get("payment_id")):
        raise ApiError(400, "Webhook has no payment")

    if event_id and not subscription_store.record_event(event_id, event):
        return {"status": "duplicate"}

    try:
        result = "ok"
        if is_subscription_event:
            row = subscription_store.get_by_razorpay_id(entity["id"])
            user_id = row["user_id"] if row else subscription_logic.owner_from_notes(entity)
            if not user_id:
                logger.info("Webhook %s for a subscription that is not ReelVault's (%s): ignored", event, entity["id"])
                result = "unknown_subscription"
            else:
                subscription_logic.apply_razorpay_subscription(user_id, entity)

        # Payment ledger: renewals arrive on subscription.charged; payment.* / refund.* arrive on their own.
        if event == "refund.processed" and not payment.get("id"):
            payment = razorpay_client.fetch_payment(refund["payment_id"])  # refund event without the payment: read it
        if payment.get("id") and (event in payment_logic.PAYMENT_EVENTS or event in payment_logic.SUBSCRIPTION_EVENTS_WITH_PAYMENT):
            payment_logic.record_payment(payment, entity or None)
        return {"status": result}
    except Exception:
        # Un-record so Razorpay's retry of this event is processed instead of skipped as a duplicate.
        if event_id:
            subscription_store.forget_event(event_id)
        raise
