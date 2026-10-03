"""Extractors for downloading reels with yt-dlp.

This module does NOT call Gemini / paid AI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pipeline.models import Platform

# Invoked as `python -m yt_dlp` (not the bare "yt-dlp" command) so it always
# resolves through the exact interpreter running this process, regardless of
# whether the venv's Scripts/bin dir is on the system PATH.
_YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]


@lru_cache(maxsize=1)
def _ffmpeg_location() -> str | None:
    """Resolve a working ffmpeg binary for yt-dlp's postprocessors (audio
    extraction, format merging). No ffmpeg is assumed to be on the system
    PATH — `imageio-ffmpeg` bundles a static binary, the same role
    `ffmpeg-static` plays in the Node/Mongo backend."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@dataclass
class ReelMetadata:
    source_url: str
    platform: Platform
    title: str
    creator: str
    thumbnail_url: str
    likes: str
    views: str
    duration_sec: float
    video_path: Path | None
    audio_path: Path | None
    width: int
    height: int
    fps: float


def detect_platform(url: str) -> Platform:
    lower = url.lower()
    if "instagram.com" in lower or "instagr.am" in lower:
        return Platform.INSTAGRAM
    if "youtube.com" in lower or "youtu.be" in lower:
        return Platform.YOUTUBE
    if "tiktok.com" in lower:
        return Platform.TIKTOK
    return Platform.OTHER


def _format_count(value: int | float | None) -> str:
    """Matches the frontend's expected display format, e.g. '14.2k', '1.8M'."""
    if not value:
        return "0"
    value = int(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return str(value)


def fetch_reel(url: str, output_dir: Path, download_video: bool = True) -> ReelMetadata:
    """Fetch reel metadata and optionally download video locally."""
    output_dir.mkdir(parents=True, exist_ok=True)

    info_cmd = [
        *_YTDLP_CMD,
        "--dump-json",
        "--no-download",
        "--no-warnings",
        url,
    ]
    result = subprocess.run(info_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr.strip() or result.stdout}")

    info = json.loads(result.stdout)
    platform = detect_platform(url)
    duration = float(info.get("duration") or 0)
    fps = _parse_fps(info)

    creator = str(info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()
    if creator and not creator.startswith("@"):
        creator = f"@{creator}"

    video_path: Path | None = None
    audio_path: Path | None = None

    if download_video:
        video_path = _download_video(url, output_dir)
        audio_path = _extract_audio(video_path, output_dir) if video_path else None

    return ReelMetadata(
        source_url=url,
        platform=platform,
        title=str(info.get("title") or info.get("description") or "Untitled Reel")[:500],
        creator=creator,
        thumbnail_url=str(info.get("thumbnail") or ""),
        likes=_format_count(info.get("like_count")),
        views=_format_count(info.get("view_count")),
        duration_sec=duration,
        video_path=video_path,
        audio_path=audio_path,
        width=int(info.get("width") or 1080),
        height=int(info.get("height") or 1920),
        fps=fps,
    )


def _parse_fps(info: dict) -> float:
    fps = info.get("fps")
    if fps:
        return float(fps)

    duration = float(info.get("duration") or 0)
    if duration > 0 and info.get("extractor") == "Instagram":
        return 30.0

    return 30.0


def _download_video(url: str, output_dir: Path) -> Path:
    out_template = str(output_dir / "reel.%(ext)s")
    cmd = [
        *_YTDLP_CMD,
        "-f",
        "best[ext=mp4]/best",
        "--no-playlist",
        "-o",
        out_template,
        url,
    ]
    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        cmd.extend(["--ffmpeg-location", ffmpeg_location])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Video download failed: {result.stderr.strip()}")

    for ext in ("mp4", "webm", "mkv"):
        candidate = output_dir / f"reel.{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Downloaded video file not found")


def _extract_audio(video_path: Path, output_dir: Path) -> Path | None:
    """Extract audio from the already-downloaded local video file via ffmpeg
    directly.

    NOTE: this used to shell out to `yt-dlp -x ... <local_path>` — but yt-dlp
    is a URL downloader, not a local file processor, and rejects a bare local
    path with "not a valid URL". That failure was silent here (caught,
    returns None, treated as non-fatal upstream), which meant transcription
    fell back to a placeholder on every single live run, no matter what
    DRY_RUN/--live said. Calling ffmpeg directly on the local file is both
    the correct approach and avoids that failure mode entirely.

    Still returns None (non-fatal upstream) if ffmpeg itself is unavailable
    or errors — audio being unextractable degrades to metadata-only
    analysis rather than failing the whole save.
    """
    ffmpeg_location = _ffmpeg_location()
    if not ffmpeg_location:
        return None

    audio_path = output_dir / "audio.m4a"
    cmd = [
        ffmpeg_location,
        "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "aac",
        "-b:a", "128k",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0 and audio_path.exists():
        return audio_path
    return None


def create_mock_metadata(url: str, duration_sec: float = 45.0, fps: float = 30.0) -> ReelMetadata:
    """Mock metadata for dry-run / cost estimation without downloading."""
    return ReelMetadata(
        source_url=url,
        platform=detect_platform(url),
        title="[dry-run] Sample Reel",
        creator="",
        thumbnail_url="",
        likes="0",
        views="0",
        duration_sec=duration_sec,
        video_path=None,
        audio_path=None,
        width=1080,
        height=1920,
        fps=fps,
    )
