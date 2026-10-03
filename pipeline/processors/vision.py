"""Frame-by-frame vision analysis via Gemini — dry-run mode by default (no API charges)."""

from __future__ import annotations

from pipeline import gemini_client
from pipeline.config import settings
from pipeline.cost.estimator import gemini_frame_tokens
from pipeline.extractors.frames import ExtractedFrame
from pipeline.models import FrameContext


def analyze_frames(
    frames: list[ExtractedFrame],
    dry_run: bool | None = None,
    frame_width: int = 768,
    frame_height: int = 1365,
) -> list[FrameContext]:
    """Analyze every extracted frame with Gemini vision, in batches.

    When dry_run=True, returns placeholder context and only estimates token usage.
    """
    dry_run = settings.dry_run if dry_run is None else dry_run

    if dry_run:
        return [_dry_run_frame(f, frame_width, frame_height) for f in frames]

    return _call_vision_api(frames)


def _dry_run_frame(frame: ExtractedFrame, frame_width: int, frame_height: int) -> FrameContext:
    tokens = gemini_frame_tokens(settings.gemini_vision_model) + 50
    return FrameContext(
        frame_index=frame.index,
        timestamp_sec=frame.timestamp_sec,
        image_path=str(frame.image_path),
        description=(
            f"[DRY-RUN] Frame at {frame.timestamp_sec:.1f}s — "
            f"vision analysis would run via {settings.gemini_vision_model}"
        ),
        ocr_text="[DRY-RUN] OCR text would be extracted here",
        objects=["[DRY-RUN] detected_objects"],
        is_scene_change=frame.is_scene_change,
        tokens_estimated=tokens,
    )


def _call_vision_api(frames: list[ExtractedFrame]) -> list[FrameContext]:
    """Live Gemini vision calls, batched across frames."""
    batch_size = max(1, settings.frames_per_vision_batch)
    contexts: list[FrameContext] = []

    for start in range(0, len(frames), batch_size):
        batch = frames[start : start + batch_size]
        batch_input = [(f.index, f.timestamp_sec, f.image_path) for f in batch]

        results = gemini_client.analyze_frame_batch(batch_input)
        results_by_index = {r.get("frame_index"): r for r in results}

        for frame in batch:
            r = results_by_index.get(frame.index, {})
            contexts.append(
                FrameContext(
                    frame_index=frame.index,
                    timestamp_sec=frame.timestamp_sec,
                    image_path=str(frame.image_path),
                    description=r.get("description", ""),
                    ocr_text=r.get("ocr_text", ""),
                    objects=r.get("objects", []) or [],
                    is_scene_change=frame.is_scene_change,
                    tokens_estimated=gemini_frame_tokens(settings.gemini_vision_model),
                )
            )

    return contexts


def aggregate_visual_context(frame_contexts: list[FrameContext]) -> str:
    """Merge per-frame descriptions into a single searchable visual narrative."""
    if not frame_contexts:
        return ""

    lines = []
    for fc in sorted(frame_contexts, key=lambda f: f.timestamp_sec):
        parts = [f"[{fc.timestamp_sec:.1f}s] {fc.description}"]
        if fc.ocr_text and "[DRY-RUN]" not in fc.ocr_text:
            parts.append(f"On-screen text: {fc.ocr_text}")
        elif fc.ocr_text:
            parts.append("On-screen text: (pending extraction)")
        if fc.objects:
            parts.append(f"Objects: {', '.join(fc.objects)}")
        lines.append(" | ".join(parts))

    return "\n".join(lines)

