"""Stops a deleted or suspended account from being used with a still-unexpired session token.

Supabase access tokens are stateless JWTs (valid until they expire, up to an hour) and deleting or suspending the user
revokes the refresh token but not tokens already issued. So every request checks the account's state: 'ok' answers are
cached for a few minutes, a change made through this backend is remembered at once, and a Supabase outage never locks
anyone out.
"""

from __future__ import annotations

import logging
import threading
import time

from pipeline.storage import account_store

logger = logging.getLogger("reelvault.account")

OK_TTL_SECONDS = 300
SUSPENDED_TTL_SECONDS = 60  # short: a lifted suspension should take effect soon even on another server instance
_lock = threading.Lock()
_state: dict[str, tuple[str, float]] = {}  # user id -> ("ok" | "suspended", cached until)
_deleted: set[str] = set()


def mark_deleted(user_id: str) -> None:
    with _lock:
        _deleted.add(user_id)
        _state.pop(user_id, None)


def mark_suspended(user_id: str) -> None:
    with _lock:
        _state[user_id] = ("suspended", time.monotonic() + SUSPENDED_TTL_SECONDS)


def clear(user_id: str) -> None:
    """Forget what we know, so the next request asks Supabase (used when a suspension is lifted)."""
    with _lock:
        _state.pop(user_id, None)


def check(user_id: str) -> str:
    """'ok' | 'suspended' | 'deleted'."""
    now = time.monotonic()
    with _lock:
        if user_id in _deleted:
            return "deleted"
        cached = _state.get(user_id)
        if cached and cached[1] > now:
            return cached[0]
    try:
        status = account_store.get_status(user_id)
    except Exception:
        logger.warning("Could not check the state of account %s; allowing the request", user_id, exc_info=True)
        return "ok"
    with _lock:
        if status == "missing":
            _deleted.add(user_id)
            return "deleted"
        ttl = SUSPENDED_TTL_SECONDS if status == "suspended" else OK_TTL_SECONDS
        _state[user_id] = (status, now + ttl)
    return status


def reset_for_tests() -> None:
    with _lock:
        _state.clear()
        _deleted.clear()
