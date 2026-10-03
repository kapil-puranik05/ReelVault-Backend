"""Minimal Razorpay REST client (Plans + Subscriptions APIs) and webhook signature check.

Talks to https://api.razorpay.com/v1 with HTTP basic auth (key id / secret).
The secret never leaves the backend.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from api.errors import ApiError
from pipeline.config import settings

logger = logging.getLogger("reelvault.razorpay")

_BASE_URL = "https://api.razorpay.com/v1"


def to_minor_units(amount: float | Decimal | str) -> int:
    """Rupees -> paise (INR has 100 minor units): 1 -> 100, 0.5 -> 50."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _require_configured() -> None:
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise ApiError(503, "Payments are not configured yet. Please try again later.")


def _request(method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_configured()
    try:
        response = httpx.request(
            method,
            f"{_BASE_URL}{path}",
            json=json,
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Razorpay request failed: %s %s: %s", method, path, exc)
        raise ApiError(502, "Could not reach the payment provider. Please try again.") from exc

    if response.status_code >= 400:
        # Razorpay's error body has a human-readable description; log it, but
        # don't forward provider internals to the client.
        logger.warning("Razorpay %s %s -> %s %s", method, path, response.status_code, response.text[:500])
        if response.status_code in (400, 404):
            raise ApiError(400, "The payment provider rejected this request.")
        raise ApiError(502, "The payment provider returned an error. Please try again.")

    return response.json()


def create_plan(amount_minor: int, currency: str) -> dict[str, Any]:
    return _request(
        "POST",
        "/plans",
        {
            "period": "monthly",
            "interval": 1,
            "item": {"name": "ReelVault Premium", "amount": amount_minor, "currency": currency},
        },
    )


def create_subscription(plan_id: str, total_count: int, user_id: str) -> dict[str, Any]:
    return _request(
        "POST",
        "/subscriptions",
        {"plan_id": plan_id, "total_count": total_count, "customer_notify": 1, "notes": {"user_id": user_id}},
    )


def fetch_subscription(subscription_id: str) -> dict[str, Any]:
    return _request("GET", f"/subscriptions/{subscription_id}")


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = settings.razorpay_webhook_secret
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
