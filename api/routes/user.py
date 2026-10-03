from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from api.deps import CurrentUser, get_current_user
from api.rate_limit import check_rate_limit

router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/me")
def get_profile(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"user:me:{user.id}", limit=60, window_seconds=60)
    # The JWT's `iat` (issued-at) approximates "last sign in" without a extra
    # round-trip to Supabase's Admin API — good enough given the frontend
    # doesn't currently call this endpoint anywhere.
    last_sign_in = (
        datetime.fromtimestamp(user.issued_at, tz=timezone.utc).isoformat() if user.issued_at else None
    )
    return {"id": user.id, "email": user.email, "lastSignIn": last_sign_in}
