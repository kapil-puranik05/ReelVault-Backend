"""Persist processed reel records as JSON (MVP; swap for Supabase later).

Also implements search: keyword search always works offline, and semantic
search (cosine similarity over Gemini embeddings) kicks in automatically.
"""

from __future__ import annotations

import math
from pathlib import Path

from pipeline.config import settings
from pipeline.models import ReelRecord


def save_reel(record: ReelRecord, output_dir: Path | None = None) -> Path:
    output_dir = output_dir or settings.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record.id}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_reel(reel_id: str, output_dir: Path | None = None) -> ReelRecord | None:
    output_dir = output_dir or settings.output_dir
    path = output_dir / f"{reel_id}.json"
    if not path.exists():
        return None
    return ReelRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_reels(output_dir: Path | None = None) -> list[ReelRecord]:
    output_dir = output_dir or settings.output_dir
    if not output_dir.exists():
        return []
    records = []
    for path in sorted(output_dir.glob("*.json")):
        records.append(ReelRecord.model_validate_json(path.read_text(encoding="utf-8")))
    return records


def delete_reel(reel_id: str, output_dir: Path | None = None) -> bool:
    output_dir = output_dir or settings.output_dir
    path = output_dir / f"{reel_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def _keyword_search(query: str, records: list[ReelRecord], limit: int = 5) -> list[tuple[float, ReelRecord]]:
    query_lower = query.lower()
    query_terms = set(query_lower.split())
    scored: list[tuple[float, ReelRecord]] = []

    for reel in records:
        haystack = reel.search_text.lower()
        score = sum(1 for term in query_terms if term in haystack)
        if query_lower in haystack:
            score += 5
        if score > 0:
            scored.append((float(score), reel))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _semantic_search(query: str, records: list[ReelRecord], limit: int = 5) -> list[tuple[float, ReelRecord]] | None:
    embedded_records = [r for r in records if r.embedding]
    if not embedded_records:
        return None

    from pipeline.processors.summarizer import embed_query

    try:
        query_embedding = embed_query(query, dry_run=False)
    except Exception:
        return None

    if not query_embedding:
        return None

    scored = [(_cosine_similarity(query_embedding, r.embedding), r) for r in embedded_records]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


def search_reels(
    query: str,
    output_dir: Path | None = None,
    limit: int = 5,
    live: bool = False,
) -> list[tuple[float, ReelRecord]]:
    records = list_reels(output_dir)
    if not records:
        return []

    if live:
        semantic_results = _semantic_search(query, records, limit=limit)
        if semantic_results:
            return semantic_results

    return _keyword_search(query, records, limit=limit)


# Backwards-compatible alias

def search_by_keywords(query: str, output_dir: Path | None = None, limit: int = 5) -> list[ReelRecord]:
    return [r for _, r in _keyword_search(query, list_reels(output_dir), limit=limit)]

