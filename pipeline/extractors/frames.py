"""OpenCV frame extraction with sampling and optional scene-change dedup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from pipeline.config import settings


@dataclass
class ExtractedFrame:
    index: int
    timestamp_sec: float
    image_path: Path
    is_scene_change: bool


def extract_frames(
    video_path: Path,
    output_dir: Path,
    sample_fps: float | None = None,
    max_frames: int | None = None,
    target_width: int | None = None,
    scene_threshold: float | None = None,
) -> tuple[list[ExtractedFrame], float, float]:
    """Extract frames from video at configurable sampling rate.

    Returns (frames, duration_sec, native_fps).
    """
    sample_fps = sample_fps if sample_fps is not None else settings.sample_fps
    max_frames = max_frames if max_frames is not None else settings.max_frames
    target_width = target_width if target_width is not None else settings.frame_width
    scene_threshold = (
        scene_threshold if scene_threshold is not None else settings.scene_change_threshold
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = total_frames / native_fps if native_fps > 0 else 0.0

    # Interval between sampled frames
    if sample_fps >= native_fps:
        frame_interval = 1
    else:
        frame_interval = max(1, int(round(native_fps / sample_fps)))

    frames: list[ExtractedFrame] = []
    prev_hist: np.ndarray | None = None
    frame_idx = 0
    saved_count = 0

    while cap.isOpened() and saved_count < max_frames:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval != 0:
            frame_idx += 1
            continue

        timestamp = frame_idx / native_fps
        resized = _resize_frame(frame, target_width)
        is_scene_change = False

        if scene_threshold > 0:
            hist = _frame_histogram(resized)
            if prev_hist is not None:
                diff = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                is_scene_change = diff * 100 > scene_threshold
            prev_hist = hist

        # Skip redundant frames in static segments (keep first + scene changes)
        if frames and not is_scene_change and scene_threshold > 0:
            if timestamp - frames[-1].timestamp_sec < 3.0:
                frame_idx += 1
                continue

        image_path = output_dir / f"frame_{saved_count:04d}_{timestamp:.2f}s.jpg"
        cv2.imwrite(str(image_path), resized, [cv2.IMWRITE_JPEG_QUALITY, 85])

        frames.append(
            ExtractedFrame(
                index=saved_count,
                timestamp_sec=round(timestamp, 2),
                image_path=image_path,
                is_scene_change=is_scene_change or saved_count == 0,
            )
        )
        saved_count += 1
        frame_idx += 1

    cap.release()
    return frames, duration_sec, native_fps


def _resize_frame(frame: np.ndarray, target_width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= target_width:
        return frame
    scale = target_width / w
    new_h = int(h * scale)
    return cv2.resize(frame, (target_width, new_h), interpolation=cv2.INTER_AREA)


def _frame_histogram(frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def plan_frame_extraction(
    duration_sec: float,
    native_fps: float = 30.0,
    sample_fps: float | None = None,
    max_frames: int | None = None,
    scene_threshold: float | None = None,
) -> dict:
    """Estimate how many frames would be extracted (no video needed)."""
    sample_fps = sample_fps if sample_fps is not None else settings.sample_fps
    max_frames = max_frames if max_frames is not None else settings.max_frames
    scene_threshold = (
        scene_threshold if scene_threshold is not None else settings.scene_change_threshold
    )

    if sample_fps >= native_fps:
        estimated = int(duration_sec * native_fps)
        mode = "every_frame"
    else:
        estimated = int(duration_sec * sample_fps)
        mode = "sampled"

    if scene_threshold > 0:
        estimated = max(1, int(estimated * 0.6))

    capped = min(estimated, max_frames)
    total_native = int(duration_sec * native_fps)

    return {
        "duration_sec": duration_sec,
        "native_fps": native_fps,
        "sample_fps": sample_fps,
        "extraction_mode": mode,
        "total_native_frames": total_native,
        "estimated_frames_before_cap": estimated,
        "estimated_frames_after_cap": capped,
        "max_frames_cap": max_frames,
    }

