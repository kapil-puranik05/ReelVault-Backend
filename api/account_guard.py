"""Stops a deleted account from being used with a still-unexpired session token.

Supabase access tokens are stateless JWTs (valid until they expire, up to an hour) and deleting the user revokes the
refresh token but not tokens already issued. So every request checks the account still exists: positive answers are
cached for a few minutes, a deletion made here is remembered at once, and a Supabase outage never locks anyone out.
"""

from __future__ import annotations

import logging
import threading
import time

from pipeline.storage import account_store

logger = logging.getLogger("reelvault.account")

_ALIVE_TTL_SECONDS = 300
_lock = threading.Lock()
_alive: dict[str, float] = {}
_deleted: set[str] = set()


def mark_deleted(user_id: str) -> None:
    with _lock:
        _deleted.add(user_id)
        _alive.pop(user_id, None)


def is_deleted(user_id: str) -> bool:
    now = time.monotonic()
    with _lock:
        if user_id in _deleted:
            return True
        if _alive.get(user_id, 0) > now:
            return False
    try:
        exists = account_store.user_exists(user_id)
    except Exception:
        logger.warning("Could not check whether account %s still exists; allowing the request", user_id, exc_info=True)
        return False
    with _lock:
        if exists:
            _alive[user_id] = now + _ALIVE_TTL_SECONDS
        else:
            _deleted.add(user_id)
    return not exists


def reset_for_tests() -> None:
    with _lock:
        _alive.clear()
        _deleted.clear()
