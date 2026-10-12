from __future__ import annotations

import logging
import re

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api import reel_jobs
from api.deps import CurrentUser, require_active_subscription
from api.errors import ApiError
from api.rate_limit import check_rate_limit
from api.schemas import SaveReelRequest, record_to_dto, search_result_to_dto
from pipeline.config import settings
from pipeline.orchestrator import process_reel
from pipeline.processors.summarizer import embed_query
from pipeline.storage import job_store, supabase_store

router = APIRouter(prefix="/api/reels", tags=["reels"])
logger = logging.getLogger("reelvault.reels")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@router.get("")
def list_reels(user: CurrentUser = Depends(require_active_subscription)):
    check_rate_limit(f"reels:list:{user.id}", limit=60, window_seconds=60)
    records = supabase_store.list_reels(user.id)
    return [record_to_dto(r) for r in records]


@router.post("/save")
def save_reel(body: SaveReelRequest, user: CurrentUser = Depends(require_active_subscription)):
    # Tighter limit than the read endpoints — each call runs the full AI pipeline.
    check_rate_limit(f"reels:save:{user.id}", limit=10, window_seconds=60)

    try:
        result = process_reel(str(body.url), dry_run=settings.dry_run, user_id=user.id, persist_json=False)
    except Exception as exc:
        logger.exception("Reel processing failed for %s", body.url)
        raise ApiError(500, f"Failed to process this reel: {exc}") from exc

    # SupabaseNotConfigured, if raised here, is handled globally (api/errors.py).
    saved = supabase_store.save_reel(result.reel)
    return record_to_dto(saved)


def _job_dto(job: dict[str, Any]) -> dict[str, Any]:
    failed = job["status"] == "FAILED"
    return {
        "id": job["id"],
        "url": job["url"],
        "status": job["status"],
        "reelId": job.get("reel_id"),
        # Internal error detail stays in the database / logs; the app only gets a plain sentence.
        "message": "We couldn't save this reel. The link may be private or unsupported." if failed else None,
        "createdAt": job["created_at"],
        "updatedAt": job["updated_at"],
    }


@router.post("/queue", status_code=202)
def queue_reel(body: SaveReelRequest, user: CurrentUser = Depends(require_active_subscription)):
    """Accepts a link and returns at once; the reel is saved by a background job, so the app may close right
    after this call. Poll GET /jobs (or just reload the library later) to see the result."""
    check_rate_limit(f"reels:queue:{user.id}", limit=10, window_seconds=60)
    url = str(body.url)

    reel_id = supabase_store.find_reel_id_by_url(user.id, url)
    if reel_id:
        return JSONResponse(status_code=200, content={"status": "ALREADY_SAVED", "reelId": reel_id})

    active = job_store.find_active_by_url(user.id, url)
    if active:
        return _job_dto(active)  # same link shared twice while it's still being processed

    if job_store.count_active(user.id) >= settings.reel_job_max_active_per_user:
        raise ApiError(429, "Too many reels are still being saved. Please wait for some to finish.")

    try:
        job = job_store.create(user.id, url)
    except Exception as exc:
        # Lost a race with an identical request: the one-active-job-per-link index rejected this insert.
        job = job_store.find_active_by_url(user.id, url) if "23505" in str(exc) else None
        if not job:
            raise
    reel_jobs.submit(job["id"])
    return _job_dto(job)


@router.get("/jobs")
def list_jobs(user: CurrentUser = Depends(require_active_subscription)):
    check_rate_limit(f"reels:jobs:{user.id}", limit=120, window_seconds=60)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return {"jobs": [_job_dto(j) for j in job_store.list_recent(user.id, since)]}


@router.delete("/jobs/{job_id}")
def dismiss_job(job_id: str, user: CurrentUser = Depends(require_active_subscription)):
    check_rate_limit(f"reels:jobs:{user.id}", limit=120, window_seconds=60)
    if not _UUID_RE.match(job_id):
        raise ApiError(400, "Invalid id")
    if not job_store.delete_finished(user.id, job_id):
        raise ApiError(404, "Job not found or still running")
    return {"success": True}


@router.get("/search")
def search_reels(q: str, user: CurrentUser = Depends(require_active_subscription)):
    check_rate_limit(f"reels:search:{user.id}", limit=30, window_seconds=60)

    if not q or not q.strip():
        raise ApiError(400, "Search query is required")
    if len(q) > 200:
        raise ApiError(400, "Search query is too long")

    query_embedding = embed_query(q.strip(), dry_run=settings.dry_run)
    if not query_embedding:
        raise ApiError(500, "Failed to generate a vector embedding for this search")

    results = supabase_store.search_reels(user.id, query_embedding, limit=5)
    return [search_result_to_dto(score, record) for score, record in results]


@router.delete("/{reel_id}")
def delete_reel(reel_id: str, user: CurrentUser = Depends(require_active_subscription)):
    check_rate_limit(f"reels:delete:{user.id}", limit=30, window_seconds=60)

    if not _UUID_RE.match(reel_id):
        raise ApiError(400, "Invalid id")

    deleted = supabase_store.delete_reel(reel_id, user.id)
    if not deleted:
        raise ApiError(404, "Reel not found")

    return {"success": True, "message": "Reel successfully deleted."}
