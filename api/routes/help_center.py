"""Help Center: users raise support queries, stored with the submitting user's name.

Deliberately not behind require_active_subscription — someone whose trial has
lapsed or whose payment failed must still be able to reach support.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.deps import CurrentUser, get_current_user
from api.rate_limit import check_rate_limit
from pipeline.storage import help_store

router = APIRouter(prefix="/api/help-center", tags=["help-center"])


class SupportQueryRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=150)
    message: str = Field(min_length=1, max_length=2000)


def _to_dto(row: dict) -> dict:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "message": row["message"],
        "status": row["status"],
        "createdAt": row["created_at"],
    }


@router.post("/queries")
def submit_query(body: SupportQueryRequest, user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"help:create:{user.id}", limit=5, window_seconds=60)
    subject, message = body.subject.strip(), body.message.strip()
    if not subject or not message:
        from api.errors import ApiError
        raise ApiError(400, "Please enter a subject and a message.")
    # The name is taken from the verified session, never from the request body.
    name = user.name or (user.email.split("@")[0] if user.email else "Unknown")
    return _to_dto(help_store.create(user.id, name, user.email, subject, message))


@router.get("/queries")
def list_my_queries(user: CurrentUser = Depends(get_current_user)):
    check_rate_limit(f"help:list:{user.id}", limit=60, window_seconds=60)
    return {"queries": [_to_dto(r) for r in help_store.list_for_user(user.id)]}
