from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    OTHER = "other"


# Must match src/data/mockData.ts `Reel['category']` in the frontend EXACTLY.
CATEGORIES = ["Tech", "Food", "Fitness", "Finance", "Productivity", "Other"]

# Must match src/data/mockData.ts `Reel['actionType']` in the frontend EXACTLY.
ACTION_TYPES = ["code", "recipe", "steps", "general"]


class TranscriptSegment(BaseModel):
    """Matches the frontend's TranscriptItem: { time: number; text: string }."""

    time: float = 0.0
    text: str = ""


class ActionData(BaseModel):
    code: str | None = None
    language: str | None = None
    ingredients: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    extracted_content: str = ""


class FrameContext(BaseModel):
    frame_index: int
    timestamp_sec: float
    image_path: str
    description: str = ""
    ocr_text: str = ""
    objects: list[str] = Field(default_factory=list)
    is_scene_change: bool = False
    tokens_estimated: int = 0


class ReelRecord(BaseModel):
    id: str
    user_id: str = ""
    source_url: str
    platform: Platform
    title: str = ""
    creator: str = ""
    thumbnail_url: str = ""
    video_url: str = ""
    likes: str = "0"
    views: str = "0"
    duration_sec: float = 0.0
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    frame_contexts: list[FrameContext] = Field(default_factory=list)
    visual_summary: str = ""
    summary: str = ""
    category: str = "Other"
    action_type: str = "general"
    action_data: ActionData = Field(default_factory=ActionData)
    tags: list[str] = Field(default_factory=list)
    search_text: str = ""
    embedding: list[float] = Field(default_factory=list)
    processing_mode: str = "dry_run"
    cost_estimate_usd: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PipelineResult(BaseModel):
    reel: ReelRecord
    frames_extracted: int
    frames_analyzed: int
    dry_run: bool
    cost_estimate_usd: dict[str, Any]
