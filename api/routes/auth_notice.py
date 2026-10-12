"""A public question the app asks after a failed sign-in: "was this account deleted?".

Supabase answers a deleted account's sign-in exactly like a wrong password, so without this the person would only see
"Invalid login credentials". The answer is limited to a one-word status; suspended accounts need no lookup (Supabase
itself refuses their sign-in with a distinct error, and only after the correct password was given).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api.errors import ApiError
from api.rate_limit import check_rate_limit
from pipeline.storage import account_store, admin_store

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


def caller_address(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return (forwarded or (request.client.host if request.client else "") or "unknown")[:64]


class NoticeRequest(BaseModel):
    email: str


@router.post("/account-notice")
def account_notice(body: NoticeRequest, request: Request):
    check_rate_limit(f"notice:{caller_address(request)}", limit=10, window_seconds=60)
    email = body.email.strip().lower()
    if len(email) > 320 or not _EMAIL.match(email):
        raise ApiError(400, "Please enter a valid email address.")

    notice = admin_store.deleted_notice(email)
    # A person who deleted their account and later signed up again with the same email has an account now: not "deleted".
    if notice and not account_store.email_has_account(email):
        return {"status": "deleted", "deletedBy": notice["deleted_by"]}
    return {"status": "none"}
