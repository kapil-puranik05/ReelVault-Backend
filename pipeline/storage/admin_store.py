"""Deletion notices and the admin audit log (backend-only tables; see supabase/account_actions.sql)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from pipeline.config import settings
from pipeline.storage.supabase_store import get_client


def hash_email(email: str) -> str:
    """Keyed hash of an email: lets us recognise it later without keeping it. Keyed with a server secret, so a copy of the
    table alone cannot be checked against a list of guessed addresses."""
    key = (settings.supabase_jwt_secret or "reelvault-dev").encode()
    return hmac.new(key, email.strip().lower().encode(), hashlib.sha256).hexdigest()


def record_deleted(email: str, user_id: str, deleted_by: str) -> None:
    get_client().table("deleted_accounts").upsert(
        {"email_hash": hash_email(email), "user_id": user_id, "deleted_by": deleted_by}, on_conflict="email_hash"
    ).execute()


def deleted_notice(email: str) -> dict[str, Any] | None:
    res = get_client().table("deleted_accounts").select("deleted_by,deleted_at").eq("email_hash", hash_email(email)).limit(1).execute()
    return res.data[0] if res.data else None


def clear_deleted(email: str) -> None:
    get_client().table("deleted_accounts").delete().eq("email_hash", hash_email(email)).execute()


def log_action(actor: str, action: str, target_user_id: str, note: str | None = None) -> None:
    get_client().table("admin_actions").insert(
        {"actor": actor, "action": action, "target_user_id": target_user_id, "note": note}
    ).execute()
