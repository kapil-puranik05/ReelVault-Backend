from __future__ import annotations

import logging
import threading
from collections import defaultdict

from fastapi import APIRouter, Depends, Request

from api import razorpay_client, subscription_logic
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
    plan_id = subscription_store.get_plan_id(amount_minor, currency)
    if plan_id:
        return plan_id
    plan = razorpay_client.create_plan(amount_minor, currency)
    subscription_store.save_plan(plan["id"], amount_minor, currency)
    return plan["id"]


@router.get("")
def get_subscription(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"sub:get:{user.id}", limit=60, window_seconds=60)
    row = subscription_logic.ensure_subscription(user.id)

    # Self-heal if a webhook was missed/delayed: a pending checkout may have been paid.
    if row.get("razorpay_subscription_id") and row["status"] == "PENDING":
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

        if existing_id:
            rz = razorpay_client.fetch_subscription(existing_id)
            row = subscription_logic.apply_razorpay_subscription(user.id, rz)
            if row["status"] == "ACTIVE":
                raise ApiError(409, "You already have an active subscription.", code="ALREADY_SUBSCRIBED")
            if rz.get("status") in subscription_logic.PAYABLE_RAZORPAY_STATUSES and rz.get("short_url"):
                # Unfinished checkout: hand back the same one rather than creating a duplicate.
                return _checkout_payload(rz)

        plan_id = _ensure_plan_id()
        rz = razorpay_client.create_subscription(plan_id, settings.paid_plan_total_count, user.id)
        if not subscription_store.claim_razorpay_subscription(user.id, rz["id"], existing_id):
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
    if not row.get("razorpay_subscription_id"):
        raise ApiError(400, "No subscription checkout has been started.")
    row = subscription_logic.sync_from_razorpay(user.id, row)
    return subscription_logic.to_dto(row)


_HANDLED_EVENTS = {
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
    if event not in _HANDLED_EVENTS:
        return {"status": "ignored"}

    entity = ((payload.get("payload") or {}).get("subscription") or {}).get("entity") or {}
    if not entity.get("id"):
        raise ApiError(400, "Webhook has no subscription")

    if event_id and not subscription_store.record_event(event_id, event):
        return {"status": "duplicate"}

    try:
        row = subscription_store.get_by_razorpay_id(entity["id"])
        user_id = row["user_id"] if row else (entity.get("notes") or {}).get("user_id")
        if not user_id:
            logger.warning("Webhook %s for unknown subscription %s", event, entity["id"])
            return {"status": "unknown_subscription"}
        subscription_logic.apply_razorpay_subscription(user_id, entity)
    except Exception:
        # Un-record so Razorpay's retry of this event is processed instead of skipped as a duplicate.
        if event_id:
            subscription_store.forget_event(event_id)
        raise

    return {"status": "ok"}
