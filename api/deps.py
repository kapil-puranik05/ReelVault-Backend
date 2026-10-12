"""Auth dependency — verifies the Supabase-issued JWT the frontend already
attaches as `Authorization: Bearer <access_token>` (see src/services/api.ts).

Supabase remains the sole identity/session provider. This never calls
Supabase's API to check the token — it only verifies its signature.

Supabase now issues two different kinds of JWT depending on when a project
was created:
  - Legacy projects: HS256, signed with a single shared secret
    (Project Settings -> API -> JWT Settings -> "JWT Secret").
  - Newer projects (this one included): ES256, signed with a private key
    Supabase holds, verified via its public JWKS endpoint
    (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`) — there is no shared
    secret at all for these, so SUPABASE_JWT_SECRET is simply the wrong tool.

This reads the token's own `alg` header to pick the right verification path,
so it works against either generation of Supabase project without config.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header
from jwt import PyJWKClient

from api import account_guard
from api.errors import ApiError
from pipeline.config import settings


@dataclass
class CurrentUser:
    id: str  # Supabase user id (the `sub` claim) — used directly as the Postgres user_id / RLS auth.uid()
    email: str
    issued_at: int | None = None  # JWT `iat` — used as a lastSignIn proxy, see routes/user.py
    name: str = ""  # display_name set at sign-up (JWT user_metadata); may be empty


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    # Cached: fetches/refreshes the JWKS over the network only occasionally,
    # not on every single request.
    return PyJWKClient(f"{settings.supabase_url}/auth/v1/.well-known/jwks.json")


def _verify(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "HS256")
    # Every Supabase-issued access token carries aud: "authenticated" — PyJWT
    # requires the expected audience be passed explicitly whenever a token
    # has an `aud` claim, or it rejects the token outright.
    # leeway: a token freshly issued by Supabase can carry an `iat` a few seconds ahead of
    # this server's clock; without it the very first request after login is rejected.
    kwargs = {"audience": "authenticated", "leeway": 10}

    if alg == "HS256":
        return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], **kwargs)

    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(token, signing_key.key, algorithms=[alg], **kwargs)


def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "Missing or malformed Authorization header")

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise ApiError(401, "Missing bearer token")

    try:
        payload = _verify(token)
    except jwt.PyJWTError:
        raise ApiError(401, "Invalid or expired session token")

    sub = payload.get("sub")
    if not sub:
        raise ApiError(401, "Token is missing a subject claim")

    state = account_guard.check(sub)
    if state == "deleted":
        raise ApiError(401, "This account has been deleted.", code="ACCOUNT_DELETED")
    if state == "suspended":
        raise ApiError(403, "Your account has been suspended.", code="ACCOUNT_SUSPENDED")

    meta = payload.get("user_metadata") or {}
    name = str(meta.get("display_name") or meta.get("name") or meta.get("full_name") or "").strip()
    return CurrentUser(id=sub, email=payload.get("email", ""), issued_at=payload.get("iat"), name=name)


def require_active_subscription(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Authenticated AND (active trial OR active paid subscription); otherwise 402.

    Reads only our own DB (kept in sync by verify + Razorpay webhooks), so the
    hot path never waits on Razorpay.
    """
    # Imported here: subscription_logic pulls in the storage layer, which this module shouldn't load at import time.
    from api import subscription_logic

    row = subscription_logic.ensure_subscription(user.id)
    if subscription_logic.access_reason(row) == "NONE":
        raise ApiError(402, "An active subscription is required.", code="SUBSCRIPTION_REQUIRED")
    return user
