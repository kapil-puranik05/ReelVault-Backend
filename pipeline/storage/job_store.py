"""Supabase persistence for the background reel-saving queue (backend-only table; see supabase/reel_jobs.sql).

Uses the service-role client (server-only), so every query filters by user_id explicitly where a user is involved,
same as the other stores. State changes are compare-and-set on `status`, so a job is never run twice at once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.storage.supabase_store import get_client

ACTIVE = ["QUEUED", "PROCESSING"]


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def create(user_id: str, url: str) -> dict[str, Any]:
    return get_client().table("reel_jobs").insert({"user_id": user_id, "url": url}).execute().data[0]


def get(job_id: str) -> dict[str, Any] | None:
    res = get_client().table("reel_jobs").select("*").eq("id", job_id).limit(1).execute()
    return res.data[0] if res.data else None


def find_active_by_url(user_id: str, url: str) -> dict[str, Any] | None:
    res = (
        get_client().table("reel_jobs").select("*")
        .eq("user_id", user_id).eq("url", url).in_("status", ACTIVE).limit(1).execute()
    )
    return res.data[0] if res.data else None


def count_active(user_id: str) -> int:
    res = get_client().table("reel_jobs").select("id").eq("user_id", user_id).in_("status", ACTIVE).execute()
    return len(res.data)


def list_recent(user_id: str, since_iso: str) -> list[dict[str, Any]]:
    res = (
        get_client().table("reel_jobs").select("*")
        .eq("user_id", user_id).gte("updated_at", since_iso).order("created_at", desc=True).limit(50).execute()
    )
    return res.data or []


def claim(job_id: str) -> dict[str, Any] | None:
    """QUEUED -> PROCESSING, only if nobody else got there first. Returns the job, or None if it wasn't ours to run."""
    job = get(job_id)
    if not job or job["status"] != "QUEUED":
        return None
    res = (
        get_client().table("reel_jobs")
        .update({"status": "PROCESSING", "attempts": job["attempts"] + 1, "updated_at": _now()})
        .eq("id", job_id).eq("status", "QUEUED").execute()
    )
    return res.data[0] if res.data else None


def finish_done(job_id: str, reel_id: str) -> None:
    get_client().table("reel_jobs").update(
        {"status": "DONE", "reel_id": reel_id, "error": None, "updated_at": _now()}
    ).eq("id", job_id).eq("status", "PROCESSING").execute()


def finish_failed(job_id: str, error: str) -> None:
    get_client().table("reel_jobs").update(
        {"status": "FAILED", "error": error[:500], "updated_at": _now()}
    ).eq("id", job_id).eq("status", "PROCESSING").execute()


def mark_notified(job_id: str) -> bool:
    """True exactly once per finished job: the caller that gets True sends the 'reel is ready' push. Compare-and-set
    on notified_at IS NULL, so retries, restarts and double completions can never notify twice."""
    res = (
        get_client().table("reel_jobs").update({"notified_at": _now()})
        .eq("id", job_id).eq("status", "DONE").is_("notified_at", "null").execute()
    )
    return len(res.data) > 0


def delete_finished(user_id: str, job_id: str) -> bool:
    """Dismiss a job from the user's list. Only finished (DONE / FAILED) jobs of that user can go."""
    res = (
        get_client().table("reel_jobs").delete()
        .eq("id", job_id).eq("user_id", user_id).in_("status", ["DONE", "FAILED"]).execute()
    )
    return len(res.data) > 0


def requeue_stale(stale_before_iso: str, max_attempts: int) -> tuple[int, int]:
    """Jobs stuck in PROCESSING (the server restarted mid-run) go back to QUEUED, or FAILED once they have been
    tried max_attempts times. Returns (requeued, failed)."""
    client = get_client()
    stuck = client.table("reel_jobs").select("id,attempts").eq("status", "PROCESSING").lt("updated_at", stale_before_iso).execute().data
    requeued = failed = 0
    for job in stuck:
        if job["attempts"] >= max_attempts:
            changed = client.table("reel_jobs").update(
                {"status": "FAILED", "error": "Gave up after repeated interruptions", "updated_at": _now()}
            ).eq("id", job["id"]).eq("status", "PROCESSING").execute().data
            failed += len(changed)
        else:
            changed = client.table("reel_jobs").update({"status": "QUEUED", "updated_at": _now()}) \
                .eq("id", job["id"]).eq("status", "PROCESSING").execute().data
            requeued += len(changed)
    return requeued, failed


def list_queued_ids() -> list[str]:
    res = get_client().table("reel_jobs").select("id").eq("status", "QUEUED").order("created_at").execute()
    return [r["id"] for r in res.data]
