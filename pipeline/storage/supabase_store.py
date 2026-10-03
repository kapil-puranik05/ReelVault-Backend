"""Production storage backend: Supabase Postgres + pgvector.

Swaps in for `json_store.py` when serving real HTTP traffic. Uses the
Supabase SERVICE ROLE key (server-side only, bypasses Row Level Security),
so every query here explicitly filters by user_id itself — RLS in
supabase/schema.sql is a second line of defense, not the only one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pipeline.config import settings
from pipeline.models import ActionData, Platform, ReelRecord, TranscriptSegment


class SupabaseNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_client():
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise SupabaseNotConfigured(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set to use Supabase storage."
        )
    from supabase import create_client

    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def _parse_embedding(value: Any) -> list[float]:
    """PostgREST has no native `vector` type support — it serializes pgvector
    columns as a string like "[-0.012,0.013,...]" instead of a real JSON
    array. Handles both that and an already-parsed list defensively."""
    if value is None:
        return []
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, str):
        stripped = value.strip("[]")
        if not stripped:
            return []
        return [float(x) for x in stripped.split(",")]
    return []


def _row_to_record(row: dict[str, Any]) -> ReelRecord:
    action_data = row.get("action_data") or {}
    return ReelRecord(
        id=str(row["id"]),
        user_id=str(row.get("user_id") or ""),
        source_url=row["source_url"],
        platform=Platform(row.get("platform") or "other"),
        title=row.get("title") or "",
        creator=row.get("creator") or "",
        thumbnail_url=row.get("thumbnail_url") or "",
        video_url=row.get("video_url") or "",
        likes=row.get("likes") or "0",
        views=row.get("views") or "0",
        duration_sec=float(row.get("duration_sec") or 0),
        transcript=[TranscriptSegment(**seg) for seg in (row.get("transcript") or [])],
        frame_contexts=[],
        visual_summary=row.get("visual_summary") or "",
        summary=row.get("summary") or "",
        category=row.get("category") or "Other",
        action_type=row.get("action_type") or "general",
        action_data=ActionData(
            code=action_data.get("code"),
            language=action_data.get("language"),
            ingredients=action_data.get("ingredients") or [],
            steps=action_data.get("steps") or [],
            extracted_content=action_data.get("extracted_content") or "",
        ),
        tags=row.get("tags") or [],
        search_text=row.get("search_text") or "",
        embedding=_parse_embedding(row.get("embedding")),
        processing_mode=row.get("processing_mode") or "live",
        cost_estimate_usd=row.get("cost_estimate_usd") or {},
        created_at=row.get("created_at"),
    )


def _record_to_row(record: ReelRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "source_url": record.source_url,
        "platform": record.platform.value,
        "title": record.title,
        "creator": record.creator,
        "thumbnail_url": record.thumbnail_url,
        "video_url": record.video_url,
        "likes": record.likes,
        "views": record.views,
        "duration_sec": record.duration_sec,
        "transcript": [seg.model_dump() for seg in record.transcript],
        "visual_summary": record.visual_summary,
        "summary": record.summary,
        "category": record.category,
        "action_type": record.action_type,
        "action_data": record.action_data.model_dump(),
        "tags": record.tags,
        "search_text": record.search_text,
        "embedding": record.embedding or None,
        "processing_mode": record.processing_mode,
        "cost_estimate_usd": record.cost_estimate_usd,
    }


def save_reel(record: ReelRecord) -> ReelRecord:
    client = get_client()
    response = client.table("reels").insert(_record_to_row(record)).execute()
    return _row_to_record(response.data[0])


def list_reels(user_id: str) -> list[ReelRecord]:
    client = get_client()
    response = (
        client.table("reels")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [_row_to_record(row) for row in response.data]


def delete_reel(reel_id: str, user_id: str) -> bool:
    client = get_client()
    response = client.table("reels").delete().eq("id", reel_id).eq("user_id", user_id).execute()
    return len(response.data) > 0


def search_reels(user_id: str, query_embedding: list[float], limit: int = 5) -> list[tuple[float, ReelRecord]]:
    """Calls the `search_reels()` pgvector function defined in supabase/schema.sql."""
    client = get_client()
    response = client.rpc(
        "search_reels",
        {"query_embedding": query_embedding, "match_user_id": user_id, "match_count": limit},
    ).execute()

    results: list[tuple[float, ReelRecord]] = []
    for row in response.data:
        similarity = float(row.pop("similarity", 0) or 0)
        # search_reels() doesn't return duration_sec/visual_summary/etc — fill in
        # harmless defaults so _row_to_record's shape stays satisfied.
        row.setdefault("duration_sec", 0)
        row.setdefault("visual_summary", "")
        row.setdefault("search_text", "")
        row.setdefault("embedding", [])
        row.setdefault("processing_mode", "live")
        row.setdefault("cost_estimate_usd", {})
        results.append((similarity, _row_to_record(row)))

    return results
