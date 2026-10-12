"""'Your reel is ready' push notifications through Expo's free push service (which relays to Android's FCM).

Best-effort by design: a notification problem must never fail or undo a saved reel. Sending needs the app to have
registered a token (see routes/user.py); without Firebase set up for the Android app, no token is ever registered and
nothing is sent from here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pipeline.config import settings
from pipeline.storage import push_store

logger = logging.getLogger("reelvault.push")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
CHANNEL_ID = "reel-processing"


def is_valid_token(token: str) -> bool:
    return len(token) < 200 and token.startswith(("ExponentPushToken[", "ExpoPushToken[")) and token.endswith("]")


def notify_reel_ready(user_id: str, job_id: str, reel_id: str, title: str = "") -> int:
    """Pushes to every device the user registered. Returns how many messages Expo accepted."""
    try:
        tokens = [t["token"] for t in push_store.list_for_user(user_id)]
        if not tokens:
            return 0
        title = (title or "").strip()
        body = f"“{title[:80]}” is ready in your vault." if title else "Your reel is ready in your vault."
        messages = [
            {
                "to": token, "title": "Reel saved", "body": body, "channelId": CHANNEL_ID, "sound": "default",
                "priority": "high", "data": {"type": "reel_ready", "reelId": reel_id, "jobId": job_id},
            }
            for token in tokens
        ]
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.expo_access_token:
            headers["Authorization"] = f"Bearer {settings.expo_access_token}"
        response = httpx.post(EXPO_PUSH_URL, json=messages, headers=headers, timeout=10.0)
        if response.status_code != 200:
            logger.warning("Expo push service answered %s", response.status_code)
            return 0
        return _handle_tickets(tokens, response.json().get("data") or [])
    except Exception:
        logger.exception("Could not send the reel-ready push for job %s", job_id)
        return 0


def _handle_tickets(tokens: list[str], tickets: list[dict[str, Any]]) -> int:
    accepted = 0
    for token, ticket in zip(tokens, tickets):
        if ticket.get("status") == "ok":
            accepted += 1
        elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
            push_store.delete_token(token)  # app uninstalled or notifications revoked: stop sending to it
        else:
            logger.warning("Push to one device was refused: %s", ticket.get("message", "unknown"))
    return accepted
