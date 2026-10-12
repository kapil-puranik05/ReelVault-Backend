from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    data_dir: Path = Field(default=Path("data"))
    frames_dir: Path = Field(default=Path("data/frames"))
    output_dir: Path = Field(default=Path("data/processed"))

    # Frame extraction — 1.0 = every second, 0.5 = every 2s, 30.0 = every native frame
    sample_fps: float = Field(default=1.0, description="Frames per second to sample")
    max_frames: int = Field(default=120, description="Cap frames per reel")
    frame_width: int = Field(default=768, description="Resize width for vision API")
    scene_change_threshold: float = Field(
        default=30.0,
        description="Histogram diff threshold for scene-change detection (0=off)",
    )
    frames_per_vision_batch: int = Field(
        default=20,
        description="How many frames to send in a single Gemini vision call (keeps prompts + "
        "responses manageable and avoids truncated JSON on long reels).",
    )

    # API behavior — NEVER call paid APIs unless explicitly enabled
    dry_run: bool = Field(default=True, description="Skip all paid API calls")

    # --- Gemini-only model configuration ---
    gemini_api_key: str = Field(default="", description="GEMINI_API_KEY from ai.google.dev")
    # "-latest" aliases instead of pinned version numbers — Google has already
    # deprecated gemini-2.5-flash for new API keys once during this project's
    # lifetime, so pinning invites this same breakage again. Using the "-lite"
    # alias specifically because the free tier's daily quota (20 req/day/model
    # as of this writing) is tracked PER MODEL — gemini-flash-latest and
    # gemini-flash-lite-latest draw from separate quota buckets, so this
    # isn't just a cost optimization, it's how you get a second independent
    # budget instead of contending with whatever else is calling the
    # non-lite model.
    gemini_vision_model: str = Field(
        default="gemini-flash-lite-latest", description="Multimodal model used for per-frame vision analysis"
    )
    gemini_audio_model: str = Field(
        default="gemini-flash-lite-latest", description="Multimodal model used for native audio transcription"
    )
    gemini_text_model: str = Field(
        default="gemini-flash-lite-latest", description="Text model used for summary/category/keyword generation"
    )
    gemini_embedding_model: str = Field(
        default="gemini-embedding-001", description="Embedding model used for semantic search"
    )
    # Route vision/audio/summary calls through OpenRouter instead of the Gemini API —
    # for models the Gemini key can't reach (e.g. gemini-2.5-flash-lite is closed to
    # new API keys). Embeddings always stay on the Gemini API so stored vectors match.
    llm_provider: str = Field(default="gemini", description="gemini | openrouter")
    openrouter_api_key: str = Field(default="", description="OPENROUTER_API_KEY from openrouter.ai")
    embedding_output_dim: int = Field(
        default=768,
        description="Output dimensionality requested from the Gemini embedding model "
        "(768 keeps pgvector indexes small; Gemini supports 128-3072 via MRL truncation).",
    )

    # Reel defaults for cost estimation when video not available
    default_reel_duration_sec: float = Field(default=45.0)
    default_reel_fps: float = Field(default=30.0)

    # --- HTTP API layer (FastAPI) ---
    # Same Supabase project the frontend already uses for auth, reused here for
    # BOTH JWT verification and Postgres+pgvector storage — no separate DB.
    supabase_url: str = Field(default="", description="https://<project>.supabase.co")
    supabase_service_role_key: str = Field(
        default="", description="Server-side key that bypasses RLS — never expose to the frontend"
    )
    supabase_jwt_secret: str = Field(
        default="", description="Used only to verify JWTs Supabase already issued; never calls Supabase auth directly"
    )

    # --- Subscription / billing (single source of truth for all pricing) ---
    trial_duration_days: int = Field(default=7)
    # Major currency units (rupees). Temporary launch price — change here or via env only.
    paid_plan_price: float = Field(default=1)
    paid_plan_currency: str = Field(default="INR")
    # Razorpay requires a finite billing-cycle count on every subscription.
    paid_plan_total_count: int = Field(default=120)
    # Future per-reel pack pricing. The limit is intentionally undecided: None = not enforced anywhere.
    reel_save_price: float = Field(default=40)
    reel_save_limit: int | None = Field(default=None)

    # Background reel saving (shared links): how many videos this server processes at once, how long a job may sit
    # in PROCESSING before it is presumed dead after a restart, and how often it is retried before giving up.
    reel_job_workers: int = Field(default=2)
    reel_job_stale_minutes: int = Field(default=15)
    reel_job_max_attempts: int = Field(default=3)
    reel_job_max_active_per_user: int = Field(default=10)

    # Shared secret between the admin dashboard (a separate website) and this backend's /api/admin endpoints.
    # Empty = those endpoints are switched off (they fail closed).
    admin_api_key: str = Field(default="", description="ADMIN_API_KEY, at least 24 characters")

    # Push notifications go through Expo's free push service. The token is only needed if "Enhanced Push Security"
    # is switched on for the Expo project; leave empty otherwise.
    expo_access_token: str = Field(default="", description="EXPO_ACCESS_TOKEN, optional")

    razorpay_key_id: str = Field(default="", description="Public key id; safe to return to clients")
    razorpay_key_secret: str = Field(default="", description="Server-side only")
    razorpay_webhook_secret: str = Field(default="", description="Secret configured on the Razorpay webhook")


settings = PipelineSettings()

