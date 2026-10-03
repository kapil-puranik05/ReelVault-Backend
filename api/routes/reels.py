from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends

from api.deps import CurrentUser, require_active_subscription
from api.errors import ApiError
from api.rate_limit import check_rate_limit
from api.schemas import SaveReelRequest, record_to_dto, search_result_to_dto
from pipeline.config import settings
from pipeline.orchestrator import process_reel
from pipeline.processors.summarizer import embed_query
from pipeline.storage import supabase_store

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
