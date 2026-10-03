"""Gemini-only pricing tables and per-reel cost estimation (no API calls)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: float
    output_per_1m: float
    notes: str = ""


PRICING: dict[str, ModelPricing] = {
    "gemini-2.0-flash": ModelPricing(0.10, 0.40, "Fast, cheap multimodal model"),
    "gemini-2.5-flash": ModelPricing(0.30, 2.50, "Higher quality multimodal, higher output cost"),
    "gemini-2.5-flash-lite": ModelPricing(
        0.10, 0.40, "Cheapest 2.5-series option — as of Sept 2026 this model id returns "
        "404 'no longer available to new users' for at least some API keys; verify access "
        "before relying on this price"
    ),
    "gemini-2.5-pro": ModelPricing(1.25, 10.00, "Highest quality, reserve for hard cases"),
    "gemini-3.1-flash-lite": ModelPricing(
        0.25, 1.50, "Cheapest flash-lite confirmed accessible as of Sept 2026 (verified live "
        "against this project's API key). Real audio input price is $0.50/1M, higher than the "
        "$0.25 used here for all modalities — slightly underestimates audio-heavy reels."
    ),
    "gemini-3.5-flash-lite": ModelPricing(
        0.30, 2.50, "Newest flash-lite as of Sept 2026; same rate across all input modalities."
    ),
    "gemini-embedding-001": ModelPricing(0.15, 0.0, "Text embeddings, input tokens only"),
}
# NOTE: any model string not in this dict silently falls back to gemini-2.5-flash's pricing
# (see _pricing_for below) instead of raising an error. This previously mispriced the
# "-latest" alias the project used before switching to the pinned ids above — a model not
# in this table gives a plausible-looking but unverified number, not a real one.

DEFAULT_MODEL = "gemini-2.5-flash"


def _pricing_for(model: str) -> ModelPricing:
    return PRICING.get(model, PRICING[DEFAULT_MODEL])


def gemini_frame_tokens(model: str, resolution: str = "default") -> int:
    if "2.5" in model:
        return 280 if resolution == "high" else 258
    return 258 if resolution == "default" else 66


def gemini_audio_tokens_per_sec(model: str) -> int:
    return 32


def estimate_reel_cost(
    duration_sec: float,
    num_frames: int,
    vision_model: str | None = None,
    text_model: str | None = None,
    embedding_model: str | None = None,
    include_audio: bool = True,
    include_summary: bool = True,
    include_embedding: bool = True,
    vision_output_tokens_per_frame: int = 80,
    summary_input_tokens: int = 2000,
    summary_output_tokens: int = 200,
    embedding_tokens: int = 500,
) -> dict:
    """Calculate estimated USD cost to fully process one reel using only Gemini APIs."""
    from pipeline.config import settings

    vision_model = vision_model or settings.gemini_vision_model
    text_model = text_model or settings.gemini_text_model
    embedding_model = embedding_model or settings.gemini_embedding_model

    duration_min = duration_sec / 60.0
    breakdown: dict[str, dict] = {}
    total = 0.0

    tokens_per_frame = gemini_frame_tokens(vision_model)
    vision_input = num_frames * (tokens_per_frame + 15)
    vision_output = num_frames * vision_output_tokens_per_frame
    pricing = _pricing_for(vision_model)
    vision_cost = (vision_input * pricing.input_per_1m + vision_output * pricing.output_per_1m) / 1_000_000
    breakdown["vision"] = {
        "model": vision_model,
        "frames": num_frames,
        "tokens_per_frame": tokens_per_frame,
        "total_input_tokens": vision_input,
        "total_output_tokens": vision_output,
        "cost_usd": round(vision_cost, 6),
    }
    total += vision_cost

    if include_audio:
        audio_tokens_per_sec = gemini_audio_tokens_per_sec(vision_model)
        audio_input = int(duration_sec * audio_tokens_per_sec)
        audio_output = int(duration_sec * 5)
        audio_cost = (audio_input * pricing.input_per_1m + audio_output * pricing.output_per_1m) / 1_000_000
        breakdown["audio_transcription"] = {
            "model": vision_model,
            "duration_sec": round(duration_sec, 1),
            "input_tokens": audio_input,
            "output_tokens": audio_output,
            "cost_usd": round(audio_cost, 6),
        }
        total += audio_cost

    if include_summary:
        text_pricing = _pricing_for(text_model)
        summary_cost = (
            summary_input_tokens * text_pricing.input_per_1m + summary_output_tokens * text_pricing.output_per_1m
        ) / 1_000_000
        breakdown["summary"] = {
            "model": text_model,
            "input_tokens": summary_input_tokens,
            "output_tokens": summary_output_tokens,
            "cost_usd": round(summary_cost, 6),
        }
        total += summary_cost

    if include_embedding:
        embed_pricing = _pricing_for(embedding_model)
        embed_cost = (embedding_tokens * embed_pricing.input_per_1m) / 1_000_000
        breakdown["embedding"] = {
            "model": embedding_model,
            "tokens": embedding_tokens,
            "cost_usd": round(embed_cost, 6),
        }
        total += embed_cost

    return {
        "total_usd": round(total, 6),
        "total_inr": round(total * 85, 4),
        "duration_sec": duration_sec,
        "frames_analyzed": num_frames,
        "breakdown": breakdown,
    }


def compare_strategies(duration_sec: float = 45.0, native_fps: float = 30.0) -> list[dict]:
    from pipeline.extractors.frames import plan_frame_extraction

    strategies = [
        ("smart_sample_1fps", 1.0),
        ("sample_0.5fps", 0.5),
        ("every_frame_30fps", 30.0),
        ("scene_only_1fps", 1.0),
    ]

    results = []
    for name, sample_fps in strategies:
        plan = plan_frame_extraction(duration_sec, native_fps, sample_fps=sample_fps)
        frames = plan["estimated_frames_after_cap"]

        for model in ("gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash"):
            cost = estimate_reel_cost(duration_sec, frames, vision_model=model)
            results.append(
                {
                    "strategy": name,
                    "sample_fps": sample_fps,
                    "vision_model": model,
                    "frames": frames,
                    "total_usd": cost["total_usd"],
                    "total_inr": cost["total_inr"],
                }
            )

    return sorted(results, key=lambda x: x["total_usd"])

