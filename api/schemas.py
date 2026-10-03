"""Request/response shapes for the HTTP layer.

`record_to_dto` is the single choke point that converts our internal
snake_case `ReelRecord` into the EXACT camelCase shape the React Native
frontend expects (src/data/mockData.ts `Reel`) — mirrors the same role
`toReelDTO` plays in the Node/Mongo backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, HttpUrl

from pipeline.models import ReelRecord


class SaveReelRequest(BaseModel):
    url: HttpUrl


def record_to_dto(record: ReelRecord) -> dict[str, Any]:
    created = record.created_at or datetime.now(timezone.utc)
    return {
        "id": record.id,
        "title": record.title,
        "creator": record.creator,
        "platform": record.platform.value,
        "videoUrl": record.video_url,
        "sourceUrl": record.source_url,
        "likes": record.likes or "0",
        "views": record.views or "0",
        "tags": record.tags,
        "summary": record.summary,
        "category": record.category,
        "actionType": record.action_type,
        "actionData": {
            "code": record.action_data.code,
            "language": record.action_data.language,
            "ingredients": record.action_data.ingredients,
            "steps": record.action_data.steps,
            "extractedContent": record.action_data.extracted_content,
        },
        "transcript": [{"time": seg.time, "text": seg.text} for seg in record.transcript],
        "savedDate": created.strftime("%Y-%m-%d"),
        "savedTime": created.strftime("%H:%M"),
    }


def search_result_to_dto(similarity: float, record: ReelRecord) -> dict[str, Any]:
    # Same 50-98% scaling the Node backend and the original Supabase RPC used —
    # keeps the frontend's "{score}% Cosine Similarity" label meaningful instead
    # of clustering near a constant (raw cosine similarity on short text tends
    # to sit close to 1.0).
    score = min(98, max(50, round(similarity * 100)))
    return {"score": score, "reel": record_to_dto(record)}
