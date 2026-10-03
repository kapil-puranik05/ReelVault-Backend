"""ReelVault Pipeline (Gemini-only)

Frame-by-frame reel processing pipeline for contextual memory and semantic search.
"""

from __future__ import annotations

import logging
import shutil
import uuid

from pipeline import gemini_client
from pipeline.config import settings
from pipeline.cost.estimator import estimate_reel_cost
from pipeline.extractors.frames import extract_frames, plan_frame_extraction
from pipeline.extractors.video import ReelMetadata, create_mock_metadata, fetch_reel
from pipeline.models import ActionData, PipelineResult, ReelRecord, TranscriptSegment
from pipeline.processors.audio import transcribe_audio
from pipeline.processors.summarizer import build_search_text, generate_embedding, generate_summary
from pipeline.processors.vision import aggregate_visual_context, analyze_frames
from pipeline.storage.json_store import save_reel

logger = logging.getLogger("reelvault.cost")


def _log_reel_cost(url: str, calls: list[dict]) -> None:
    if not calls:
        return
    total = sum(c["cost_usd"] for c in calls)
    steps: dict[str, float] = {}
    for c in calls:
        steps[c["step"]] = steps.get(c["step"], 0.0) + c["cost_usd"]
    parts = ", ".join(f"{k} ₹{v * 85:.3f}" for k, v in steps.items())
    tokens_in = sum(c["input_tokens"] for c in calls)
    tokens_out = sum(c["output_tokens"] for c in calls)
    logger.info(
        "Reel cost ₹%.3f ($%.5f) | %s | %d API calls, %d in / %d out tokens | %s",
        total * 85, total, parts, len(calls), tokens_in, tokens_out, url,
    )


def process_reel(
    url: str,
    *,
    dry_run: bool | None = None,
    download: bool = True,
    duration_sec: float | None = None,
    user_id: str = "",
    persist_json: bool = True,
) -> PipelineResult:
    """Full pipeline for one reel.

    Downloaded video/audio/frame files live under a per-reel temp directory for
    the duration of processing only — `finally` below deletes the whole
    directory once the pipeline finishes (success or failure), so nothing but
    the resulting text/metadata record is ever kept, in line with not storing
    downloaded video/audio content beyond the processing step.
    """
    dry_run = settings.dry_run if dry_run is None else dry_run
    reel_id = str(uuid.uuid4())
    work_dir = settings.data_dir / reel_id
    frames_dir = work_dir / "frames"
    work_dir.mkdir(parents=True, exist_ok=True)
    gemini_client.start_usage_tracking()

    try:
        metadata: ReelMetadata
        if download and not dry_run:
            metadata = fetch_reel(url, work_dir, download_video=True)
        elif download:
            try:
                metadata = fetch_reel(url, work_dir, download_video=True)
            except Exception:
                metadata = create_mock_metadata(
                    url,
                    duration_sec or settings.default_reel_duration_sec,
                    settings.default_reel_fps,
                )
        else:
            metadata = create_mock_metadata(
                url,
                duration_sec or settings.default_reel_duration_sec,
                settings.default_reel_fps,
            )

        extracted_frames = []
        actual_duration = metadata.duration_sec

        if metadata.video_path and metadata.video_path.exists():
            extracted_frames, actual_duration, _native_fps = extract_frames(metadata.video_path, frames_dir)

        plan = plan_frame_extraction(metadata.duration_sec, metadata.fps)
        frame_count = len(extracted_frames) if extracted_frames else plan["estimated_frames_after_cap"]

        frame_contexts = (
            analyze_frames(
                extracted_frames,
                dry_run=dry_run,
                frame_width=settings.frame_width,
                frame_height=int(settings.frame_width * 16 / 9),
            )
            if extracted_frames
            else []
        )

        if not frame_contexts and dry_run:
            from pipeline.cost.estimator import gemini_frame_tokens
            from pipeline.models import FrameContext

            for i in range(plan["estimated_frames_after_cap"]):
                frame_contexts.append(
                    FrameContext(
                        frame_index=i,
                        timestamp_sec=round(i / settings.sample_fps, 2),
                        image_path=f"[planned] frame_{i:04d}",
                        description="[DRY-RUN] planned frame",
                        tokens_estimated=gemini_frame_tokens(settings.gemini_vision_model),
                    )
                )

        visual_context = aggregate_visual_context(frame_contexts)

        transcript_segments: list[TranscriptSegment] = transcribe_audio(metadata.audio_path, dry_run=dry_run)
        transcript_text = " ".join(seg.text for seg in transcript_segments)

        analysis = generate_summary(transcript_text, visual_context, dry_run=dry_run)

        search_text = build_search_text(
            metadata.title,
            transcript_text,
            visual_context,
            analysis["summary"],
            analysis["tags"],
        )

        embedding = generate_embedding(search_text, dry_run=dry_run)

        cost = estimate_reel_cost(actual_duration or metadata.duration_sec, frame_count, vision_model=settings.gemini_vision_model)

        record = ReelRecord(
            id=reel_id,
            user_id=user_id,
            source_url=url,
            platform=metadata.platform,
            title=metadata.title,
            creator=metadata.creator,
            thumbnail_url=metadata.thumbnail_url,
            video_url="",
            likes=metadata.likes,
            views=metadata.views,
            duration_sec=actual_duration or metadata.duration_sec,
            transcript=transcript_segments,
            frame_contexts=frame_contexts,
            visual_summary=visual_context,
            summary=analysis["summary"],
            category=analysis["category"],
            action_type=analysis["action_type"],
            action_data=ActionData(**analysis["action_data"]),
            tags=analysis["tags"],
            search_text=search_text,
            embedding=embedding,
            processing_mode="dry_run" if dry_run else "live",
            cost_estimate_usd=cost,
        )

        if persist_json:
            save_reel(record)

        _log_reel_cost(url, gemini_client.pop_usage())
        return PipelineResult(
            reel=record,
            frames_extracted=len(extracted_frames),
            frames_analyzed=len(frame_contexts),
            dry_run=dry_run,
            cost_estimate_usd=cost,
        )
    finally:
        gemini_client.pop_usage()  # no-op after success; drops the partial log if the pipeline failed
        # Downloaded video/audio/frame images are never kept beyond processing —
        # only the resulting ReelRecord (text + metadata) survives past this call.
        shutil.rmtree(work_dir, ignore_errors=True)


def print_pipeline_report(result: PipelineResult) -> None:
    r = result.reel
    c = result.cost_estimate_usd

    print("\n" + "=" * 60)
    print("  ReelVault Pipeline Report")
    print("=" * 60)
    print(f"  Mode:        {'DRY-RUN (no API charges)' if result.dry_run else 'LIVE'}")
    print(f"  Reel ID:     {r.id}")
    print(f"  Platform:    {r.platform.value}")
    print(f"  Duration:    {r.duration_sec:.1f}s")
    print(f"  Frames:      {result.frames_extracted} extracted, {result.frames_analyzed} analyzed")
    print(f"  Category:    {r.category}")
    print(f"  Summary:     {r.summary[:120]}...")
    print(f"  Tags:        {', '.join(r.tags)}")
    print("-" * 60)
    print("  COST ESTIMATE (1 reel)")
    print(f"  Total:       ${c['total_usd']:.4f} USD  (~Rs.{c['total_inr']:.2f})")
    for step, details in c.get("breakdown", {}).items():
        print(f"    {step:12s} ${details['cost_usd']:.4f}")
    print("=" * 60 + "\n")
