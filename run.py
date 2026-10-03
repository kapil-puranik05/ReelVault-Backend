#!/usr/bin/env python3
"""CLI entry point for ReelVault pipeline."""

from __future__ import annotations

import argparse
import sys

from pipeline.cost.estimator import compare_strategies, estimate_reel_cost
from pipeline.extractors.frames import plan_frame_extraction
from pipeline.config import settings
from pipeline.orchestrator import print_pipeline_report, process_reel
from pipeline.storage.json_store import search_reels



def cmd_process(args: argparse.Namespace) -> None:
    result = process_reel(
        args.url,
        dry_run=not args.live,
        download=not args.no_download,
        duration_sec=args.duration,
    )
    print_pipeline_report(result)
    if args.json:
        print(result.model_dump_json(indent=2))


def cmd_cost(args: argparse.Namespace) -> None:
    duration = args.duration or settings.default_reel_duration_sec
    fps = args.fps or settings.default_reel_fps
    sample = args.sample_fps or settings.sample_fps

    plan = plan_frame_extraction(duration, fps, sample_fps=sample)
    frames = plan["estimated_frames_after_cap"]

    cost = estimate_reel_cost(
        duration,
        frames,
        vision_model=args.model or settings.gemini_vision_model,
        include_audio=not args.no_audio,
        include_summary=not args.no_summary,
        include_embedding=not args.no_embedding,
    )

    print("\n" + "=" * 60)
    print("  ReelVault Cost Estimate — 1 Reel (Gemini only)")
    print("=" * 60)
    print(f"  Duration:     {duration}s ({duration/60:.1f} min)")
    print(f"  Native FPS:   {fps}")
    print(f"  Sample rate:  {sample} fps")
    print(f"  Est. frames:  {frames} (cap: {settings.max_frames})")
    print(f"  Model:        {args.model or settings.gemini_vision_model}")
    print("-" * 60)
    for step, d in cost["breakdown"].items():
        extra = ""
        if "frames" in d:
            extra = f" ({d['frames']} frames)"
        elif "duration_sec" in d:
            extra = f" ({d['duration_sec']}s)"
        print(f"  {step:20s} ${d['cost_usd']:.6f}{extra}")
    print("-" * 60)
    print(f"  TOTAL:        ${cost['total_usd']:.4f} USD")
    print(f"                ~Rs.{cost['total_inr']:.2f}")
    print("=" * 60)

    if args.compare:
        print("\nStrategy comparison (cheapest first):\n")
        rows = compare_strategies(duration, fps)
        for row in rows[:9]:
            print(
                f"  {row['strategy']:22s} | {row['vision_model']:22s} | "
                f"{row['frames']:3d} frames | ${row['total_usd']:.4f} | Rs.{row['total_inr']:.2f}"
            )


def cmd_search(args: argparse.Namespace) -> None:
    results = search_reels(args.query, limit=args.limit, live=args.live)
    if not results:
        print("No matching reels found.")
        return
    for score, r in results:
        print(f"\n[{r.category}] {r.title}  (score: {score:.3f})")
        print(f"  {r.summary[:150]}")
        print(f"  Tags: {', '.join(r.tags)}")
        print(f"  URL: {r.source_url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ReelVault frame processing pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_process = sub.add_parser("process", help="Process a reel URL (dry-run by default)")
    p_process.add_argument("url", help="Reel URL (Instagram/YouTube)")
    p_process.add_argument("--live", action="store_true", help="Enable live API calls (costs money)")
    p_process.add_argument("--no-download", action="store_true", help="Skip yt-dlp download")
    p_process.add_argument("--duration", type=float, help="Override duration for mock runs")
    p_process.add_argument("--json", action="store_true", help="Print full JSON output")
    p_process.set_defaults(func=cmd_process)

    p_cost = sub.add_parser("cost", help="Estimate cost to process 1 reel (no API calls)")
    p_cost.add_argument("--duration", type=float, default=45.0, help="Reel duration in seconds")
    p_cost.add_argument("--fps", type=float, default=30.0, help="Native video FPS")
    p_cost.add_argument("--sample-fps", type=float, help="Frame sampling rate")
    p_cost.add_argument("--model", help="Gemini model to estimate with (e.g. gemini-2.0-flash)")
    p_cost.add_argument("--no-audio", action="store_true", help="Exclude audio transcription cost")
    p_cost.add_argument("--no-summary", action="store_true")
    p_cost.add_argument("--no-embedding", action="store_true")
    p_cost.add_argument("--compare", action="store_true", help="Compare strategies")
    p_cost.set_defaults(func=cmd_cost)

    p_search = sub.add_parser("search", help="Search saved reels by keyword or natural-language context")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument(
        "--live",
        action="store_true",
        help="Use Gemini embeddings for semantic search (requires GEMINI_API_KEY + saved "
        "reels that were processed with --live). Without this flag, falls back to keyword search.",
    )
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
