"""In-memory fixed-window rate limiter — no Redis, same tradeoff as the
Node/Mongo backend: correct for a single-instance MVP, resets on restart,
not shared across multiple instances."""

from __future__ import annotations

import time

from api.errors import ApiError

_buckets: dict[str, tuple[int, float]] = {}


def check_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    count, reset_at = _buckets.get(key, (0, now + window_seconds))

    if now > reset_at:
        _buckets[key] = (1, now + window_seconds)
        return

    count += 1
    _buckets[key] = (count, reset_at)
    if count > limit:
        raise ApiError(429, "Too many requests — please try again in a moment.")
