"""Account deletion. Retryable rather than atomic: Razorpay and Supabase can't share a transaction, so the order is
chosen so that every failure leaves the user able to simply try again, and never leaves them billed without an account."""

from __future__ import annotations

import logging

from api import account_guard, razorpay_client, subscription_logic
from api.deps import CurrentUser
from api.errors import ApiError
from pipeline.storage import account_store, subscription_store

logger = logging.getLogger("reelvault.account")

# Razorpay subscription states that can still take money. (halted = retries exhausted, paused = on hold, and
# created / cancelled / completed / expired never charge again.)
_BILLABLE = {"active", "authenticated", "pending"}


def _stop_billing(user_id: str) -> None:
    row = subscription_store.get(user_id)
    sub_id = (row or {}).get("razorpay_subscription_id")
    if not sub_id or not subscription_logic.same_mode(row):
        return  # nothing at Razorpay in the current key mode that could bill this user

    try:
        rz = razorpay_client.fetch_subscription(sub_id)
    except ApiError as exc:
        if exc.status_code == 400:
            return  # Razorpay doesn't know it: nothing to cancel
        raise
    owner = (rz.get("notes") or {}).get("user_id")
    if owner and owner != user_id:
        logger.error("Razorpay subscription %s belongs to %s, not %s; not touching it", sub_id, owner, user_id)
        return
    if rz.get("status") in _BILLABLE:
        try:
            razorpay_client.cancel_subscription(sub_id)
        except ApiError as exc:
            logger.warning("Could not cancel Razorpay subscription %s for deleting account %s: %s", sub_id, user_id, exc.message)
            raise ApiError(
                502,
                "We couldn't cancel your subscription, so your account was not deleted. "
                "Please try again in a moment, or contact support.",
                code="SUBSCRIPTION_CANCEL_FAILED",
            ) from exc


def delete_account(user: CurrentUser) -> None:
    """Deletes the signed-in user's own account. The id comes from the verified session token, never from the request."""
    _stop_billing(user.id)               # 1. stop future charges (idempotent: repeating it is harmless)
    account_store.delete_auth_user(user.id)  # 2. remove the auth user; foreign keys cascade all of their data
    account_guard.mark_deleted(user.id)  # 3. reject any still-valid session token immediately
    logger.info("Account %s deleted", user.id)
