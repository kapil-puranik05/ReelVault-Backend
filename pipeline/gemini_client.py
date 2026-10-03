"""
Single choke point for every Gemini API call in the pipeline.

Only this module imports the `google-genai` SDK. Every other module (vision.py,
audio.py, summarizer.py) calls functions here — that keeps the "only Gemini,
nowhere else" rule easy to audit, and keeps the SDK import lazy so `dry_run`
mode never requires the package or an API key to be present.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pipeline.config import settings

_client = None

# Per-reel usage log: process_reel() starts tracking, every API call below appends to it.
_usage: ContextVar[list[dict] | None] = ContextVar("reel_usage", default=None)


def start_usage_tracking() -> None:
    _usage.set([])


def pop_usage() -> list[dict]:
    """Return the calls recorded since start_usage_tracking() and stop tracking."""
    calls = _usage.get() or []
    _usage.set(None)
    return calls


def _record_usage(step: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float | None) -> None:
    calls = _usage.get()
    if calls is None:
        return
    if cost_usd is None:  # no billed cost from the provider — price the tokens from the estimator's table
        from pipeline.cost.estimator import _pricing_for

        price = _pricing_for(model)
        cost_usd = (input_tokens * price.input_per_1m + output_tokens * price.output_per_1m) / 1_000_000
    calls.append(
        {"step": step, "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost_usd}
    )


class GeminiNotConfigured(RuntimeError):
    """Raised when a live Gemini call is attempted without an API key / SDK."""


def get_client():
    """Lazily create and cache the google-genai client."""
    global _client
    if _client is not None:
        return _client

    if not settings.gemini_api_key:
        raise GeminiNotConfigured(
            "GEMINI_API_KEY is not set. Add it to your .env file "
            "(get one at https://aistudio.google.com/apikey)."
        )

    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover
        raise GeminiNotConfigured(
            "google-genai is not installed. Run: pip install google-genai"
        ) from exc

    _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _openrouter_content(parts: list[Any]) -> list[dict]:
    """Convert Gemini-style parts (str / {"inline_data": ...}) to OpenAI-style content blocks."""
    content: list[dict] = []
    for part in parts:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
            continue
        inline = part["inline_data"]
        b64 = base64.b64encode(inline["data"]).decode("ascii")
        mime_type = inline["mime_type"]
        if mime_type.startswith("image/"):
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
        else:
            audio_format = (mimetypes.guess_extension(mime_type) or ".m4a").lstrip(".")
            if audio_format == "mp4":
                audio_format = "m4a"
            content.append({"type": "input_audio", "input_audio": {"data": b64, "format": audio_format}})
    return content


def _generate(model: str, parts: list[Any], temperature: float, step: str = "call") -> str:
    """Run one JSON-mode generation on the configured provider and return the raw text."""
    if settings.llm_provider != "openrouter":
        response = get_client().models.generate_content(
            model=model,
            contents=parts,
            config={"response_mime_type": "application/json", "temperature": temperature},
        )
        meta = response.usage_metadata
        if meta:
            _record_usage(step, model, meta.prompt_token_count or 0, meta.candidates_token_count or 0, None)
        return response.text

    if not settings.openrouter_api_key:
        raise GeminiNotConfigured("LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set in .env.")

    import httpx

    response = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={
            "model": model if "/" in model else f"google/{model}",
            "messages": [{"role": "user", "content": _openrouter_content(parts)}],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "usage": {"include": True},  # ask OpenRouter to report the actual cost it charged
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:300]}")
    body = response.json()
    usage = body.get("usage") or {}
    _record_usage(step, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("cost"))
    return body["choices"][0]["message"]["content"]


def _extract_json(text: str) -> Any:
    """Gemini sometimes wraps JSON in ```json fences even with response_mime_type set."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())


def _generate_json(model: str, parts: list[Any], temperature: float, step: str, attempts: int = 3) -> Any:
    """_generate + parse, retrying when the model returns malformed JSON (smaller models do, intermittently)."""
    for attempt in range(1, attempts + 1):
        try:
            return _extract_json(_generate(model, parts, temperature, step))
        except json.JSONDecodeError:
            if attempt == attempts:
                raise


def _as_text(value: Any) -> str:
    """Smaller models sometimes return a list of lines where a string is expected."""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return "" if value is None else str(value)


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        value = [value]
    return [item if isinstance(item, str) else _as_text(item) for item in value]


def _as_list(data: Any) -> Any:
    """OpenRouter's JSON mode can wrap an array in an object ({"frames": [...]}); unwrap it."""
    if isinstance(data, dict) and len(data) == 1:
        (value,) = data.values()
        if isinstance(value, list):
            return value
    return data


# ---------------------------------------------------------------------------
# Vision: batched frame-by-frame analysis
# ---------------------------------------------------------------------------

FRAME_BATCH_PROMPT = """You are analyzing consecutive frames sampled from a short-form \
video reel, in order. Each image is labeled with its index and timestamp before it.

For EVERY frame provided, return one JSON object with these fields:
- frame_index: integer, matches the label
- description: 1 sentence describing what is visually happening
- ocr_text: any visible on-screen text/captions/slides (empty string if none)
- objects: short list of key objects, people, or UI elements visible
- scene_type: one of tutorial | demo | talking_head | text_slide | product | other

Return ONLY a JSON array of these objects, one per frame, in the same order as given. \
No prose, no markdown fences.
"""


def analyze_frame_batch(image_paths_with_meta: list[tuple[int, float, Path]]) -> list[dict]:
    """
    Send a batch of frame images to Gemini in a single multimodal call.

    image_paths_with_meta: list of (frame_index, timestamp_sec, image_path)
    Returns a list of dicts: {frame_index, description, ocr_text, objects, scene_type}
    """
    parts: list[Any] = [FRAME_BATCH_PROMPT]
    for frame_index, timestamp_sec, image_path in image_paths_with_meta:
        parts.append(f"Frame index {frame_index}, timestamp {timestamp_sec:.2f}s:")
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": Path(image_path).read_bytes(),
                }
            }
        )

    data = _as_list(_generate_json(settings.gemini_vision_model, parts, temperature=0.1, step="vision"))
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array from Gemini vision call, got: {type(data)}")
    return data


# ---------------------------------------------------------------------------
# Audio: native Gemini audio understanding (no separate Whisper call needed)
# ---------------------------------------------------------------------------

AUDIO_TRANSCRIPT_PROMPT = """Transcribe the spoken audio in this clip, in the original \
language. Return ONLY a raw JSON array of objects shaped like [{"time": 0, "text": "..."}], \
one object per spoken phrase/sentence, where "time" is the integer start second of that \
phrase. If there is no speech (e.g. music only), return an empty array []. No markdown \
fences, no commentary."""


def transcribe_audio(audio_path: Path) -> list[dict]:
    """Transcribe audio using Gemini's native audio understanding (replaces Whisper).

    Returns a list of {"time": float, "text": str} segments — matches the frontend's
    TranscriptItem shape directly, so no separate timestamp-alignment step is needed.
    """
    mime_type = mimetypes.guess_type(str(audio_path))[0] or "audio/mp4"
    audio_bytes = Path(audio_path).read_bytes()

    parts = [AUDIO_TRANSCRIPT_PROMPT, {"inline_data": {"mime_type": mime_type, "data": audio_bytes}}]
    data = _as_list(_generate_json(settings.gemini_audio_model, parts, temperature=0.0, step="audio"))
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array from Gemini audio call, got: {type(data)}")

    return [
        {"time": float(seg.get("time", 0) or 0), "text": str(seg.get("text", "")).strip()}
        for seg in data
        if isinstance(seg, dict) and str(seg.get("text", "")).strip()
    ]


# ---------------------------------------------------------------------------
# Text: summary + category + keywords
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """You are analyzing a saved short-form video reel for ReelVault, a \
personal knowledge app. Given the audio transcript and per-frame visual descriptions \
(including any on-screen text/OCR), return a single JSON object with:
- "summary": a 2-3 sentence plain-English summary of what the reel teaches or shows.
- "category": exactly one of Tech, Food, Fitness, Finance, Productivity, Other.
- "action_type": exactly one of code, recipe, steps, general — "code" for programming \
snippets (especially if OCR caught code on screen), "recipe" for cooking ingredients/steps, \
"steps" for a checklist/procedure, "general" for high-level advice with no concrete steps.
- "action_data": populate only fields relevant to action_type ("code" + "language" for \
code — use OCR'd code if present, "ingredients" + "steps" for recipe, "steps" for \
steps/general). Always include "extracted_content": a structured breakdown using numbered \
sections ("1. Section Title") and "- bullet" items.
- "tags": 5-10 short lowercase search keywords/phrases a user might type months later to \
find this reel again (include specific tools, techniques, or dish/exercise names mentioned).

Respond with ONLY the JSON object, no markdown fencing.
"""


def generate_summary(transcript: str, visual_context: str) -> dict:
    """Generate summary/category/actionType/actionData/tags with Gemini text generation."""
    prompt = (
        f"{SUMMARY_SYSTEM_PROMPT}\n\n"
        f"AUDIO TRANSCRIPT:\n{transcript or '(no speech detected)'}\n\n"
        f"VISUAL FRAME-BY-FRAME CONTEXT (including OCR):\n{visual_context or '(no frames analyzed)'}"
    )

    data = _generate_json(settings.gemini_text_model, [prompt], temperature=0.3, step="summary")
    action_data = data.get("action_data") or {}
    return {
        "summary": data.get("summary", ""),
        "category": data.get("category", "Other"),
        "action_type": data.get("action_type", "general"),
        "action_data": {
            "code": _as_text(action_data.get("code")) or None,
            "language": action_data.get("language"),
            "ingredients": _as_str_list(action_data.get("ingredients")),
            "steps": _as_str_list(action_data.get("steps")),
            "extracted_content": _as_text(action_data.get("extracted_content")),
        },
        "tags": data.get("tags", []),
    }


# ---------------------------------------------------------------------------
# Embeddings: semantic search vectors
# ---------------------------------------------------------------------------


def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    """Generate an embedding vector via Gemini's embedding model."""
    client = get_client()

    response = client.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=text,
        config={
            "task_type": task_type,
            "output_dimensionality": settings.embedding_output_dim,
        },
    )
    _record_usage("embedding", settings.gemini_embedding_model, len(text) // 4, 0, None)
    return list(response.embeddings[0].values)

