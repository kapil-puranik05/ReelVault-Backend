# ReelVault Pipeline (Gemini-only)

Frame-by-frame reel processing pipeline for contextual memory and semantic search.
Every AI call in this pipeline — vision, audio transcription, summarization, and
embeddings — goes through the **Gemini API**. No OpenAI, no Whisper, no other provider.

**Dry-run by default** — no paid API calls unless you pass `--live` (and set `GEMINI_API_KEY`).

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and add GEMINI_API_KEY (from https://aistudio.google.com/apikey)

# Estimate cost for 1 typical 45s reel (NO API calls)
python run.py cost --duration 45 --compare

# Process a reel in dry-run mode (extracts real frames locally, but skips Gemini calls)
python run.py process "https://www.instagram.com/reel/EXAMPLE/"

# Process for real — calls Gemini for vision, audio, summary, and embeddings
python run.py process "https://www.instagram.com/reel/EXAMPLE/" --live

# Search saved reels — semantic search via Gemini embeddings (needs --live reels + API key)
python run.py search "that trick about api rate limits" --live

# Search saved reels — offline keyword fallback (no API calls)
python run.py search "claude rate limits"
```

## Pipeline Steps (Gemini end-to-end)

```
Reel URL
   │
   ▼
yt-dlp ────────────► Download video + audio + metadata (free, no AI)
   │
   ▼
OpenCV ────────────► Extract frames at 1fps + scene-change dedup (free, no AI)
   │
   ▼
Gemini (vision) ───► Frame-by-frame: description, OCR text, objects, scene type
   │                  Frames are sent in batches (default 20/call) so every frame
   │                  still gets its own analysis, without one round-trip per frame.
   ▼
Gemini (audio) ────► Native audio transcription — Gemini accepts audio directly,
   │                  no separate Whisper step needed.
   ▼
Gemini (text) ─────► Summary (2-3 sentences) + category + search keywords,
   │                  generated from the transcript + all frame descriptions together.
   ▼
Gemini (embeddings)► Vector embedding of the reel's full search text, via
   │                  gemini-embedding-001 (RETRIEVAL_DOCUMENT task type).
   ▼
JSON / Supabase ───► Stored as a searchable record (see supabase/schema.sql)
```

**Output**: each processed reel produces one `ReelRecord` whose most important field
is `summary` — a plain-English 2-3 sentence description of what the reel shows/teaches,
plus `category`, `keywords`, the full `transcript`, and per-frame `frame_contexts`.

## How search works

- **Keyword search** (default, always available, $0): substring/term match over each
  reel's `search_text` (title + summary + keywords + transcript + visual context).
- **Semantic search** (`--live` flag): the query is embedded with Gemini
  (`RETRIEVAL_QUERY` task type) and ranked against each reel's stored embedding by
  cosine similarity — this is what lets "that trick about API rate limits" match a
  reel that never uses those exact words. In Supabase/production this same ranking
  is done server-side by the `search_reels()` pgvector function in `supabase/schema.sql`.

## Cost Per Reel (45 seconds, 1fps sampling, ~35 frames, gemini-2.5-flash)

Run `python run.py cost --compare` for a live breakdown across models and sampling
rates — these numbers move as Gemini pricing changes, so treat the table below as
illustrative only (check https://ai.google.dev/gemini-api/docs/pricing for current rates):

| Step | Model | What it does |
|------|-------|---------------|
| Frame vision (batched) | gemini-2.5-flash | Per-frame description, OCR, objects |
| Audio transcription | gemini-2.5-flash | Native audio understanding, no Whisper |
| Summary + category + keywords | gemini-2.5-flash | One text call over transcript + all frames |
| Search embedding | gemini-embedding-001 | 768-dim vector per reel |

> **Avoid every-frame at 30fps.** Sampling at 1fps with scene-change dedup keeps a
> 45s reel to ~30-40 frames instead of 1,350 — use `python run.py cost --compare` to
> see the cost difference across sampling strategies before going live.

## HTTP API (FastAPI) — callable by the React Native app

This pipeline is wrapped in a real HTTP server matching the exact contract the
frontend already calls (`src/services/api.ts`) — same 5 endpoints, same request/
response shapes, same Supabase JWT auth as the Node/Mongo backend. Storage here
is Postgres + pgvector via the **same Supabase project** already used for auth
(`supabase/schema.sql`) — no MongoDB, no separate database account.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/reels/save` | Accepts URL, runs the full vision+audio pipeline, persists to Supabase |
| GET | `/api/reels` | List the authenticated user's saved reels |
| GET | `/api/reels/search?q=` | Semantic search via the `search_reels()` pgvector function |
| DELETE | `/api/reels/:id` | Delete a reel (ownership-checked) |
| GET | `/api/user/me` | Current user profile |

### Run it

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in GEMINI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_JWT_SECRET
# then run supabase/schema.sql once in the Supabase SQL editor

uvicorn api.main:app --reload --port 8000
```

Point the Expo app's `EXPO_PUBLIC_BACKEND_URL` at `http://localhost:8000` to test
it end-to-end — no frontend changes needed, same as the Node/Mongo backend.

### Structure

```
api/
  main.py            # FastAPI app, CORS, router mounting
  deps.py            # Supabase JWT verification -> CurrentUser
  errors.py          # Centralized error handling -> {"error": string} JSON
  rate_limit.py       # In-memory per-user rate limiting (no Redis)
  schemas.py          # ReelRecord -> frontend Reel DTO mapping
  routes/
    reels.py          # save / list / search / delete
    user.py           # profile
pipeline/
  storage/
    json_store.py      # local JSON files — used by the CLI, dry-run/offline
    supabase_store.py   # Postgres + pgvector — used by the HTTP API
  ...                  # extractors/processors/orchestrator, unchanged in spirit
```

The CLI (`run.py`) still works exactly as before, using `json_store.py` — the
two storage backends are independent, so local CLI experimentation never
touches your Supabase data.

## Configuration

All settings live in `.env` (see `.env.example`):

```
DRY_RUN=true
GEMINI_API_KEY=
GEMINI_VISION_MODEL=gemini-2.5-flash
GEMINI_AUDIO_MODEL=gemini-2.5-flash
GEMINI_TEXT_MODEL=gemini-2.5-flash
GEMINI_EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_OUTPUT_DIM=768
SAMPLE_FPS=1.0
MAX_FRAMES=120
FRAME_WIDTH=768
FRAMES_PER_VISION_BATCH=20
```

- Lower `SAMPLE_FPS` or raise `FRAMES_PER_VISION_BATCH` to cut cost further.
- `EMBEDDING_OUTPUT_DIM` can be any value from 128-3072 (Gemini supports Matryoshka
  truncation) — 768 keeps the pgvector index small while still being accurate.

## Project Structure

```
pipeline/
  extractors/       # yt-dlp download, OpenCV frame extraction (no AI, free)
  processors/
    vision.py        # Batched Gemini vision calls, frame-by-frame
    audio.py          # Gemini native audio transcription
    summarizer.py      # Gemini text summary/category/keywords + Gemini embeddings
  gemini_client.py    # The ONLY module that imports/calls the Gemini SDK
  cost/               # Gemini-only pricing tables + per-reel estimator
  storage/            # JSON store (MVP) + keyword/semantic hybrid search
  orchestrator.py     # Wires the full pipeline together
supabase/schema.sql   # Postgres + pgvector(768) + reel_frames table + search_reels()
run.py                # CLI: process / cost / search
```

## Safety rails

- `DRY_RUN=true` by default — running `python run.py process <url>` never calls Gemini
  until you pass `--live`.
- `gemini_client.py` is the single choke point for all Gemini calls, so it's easy to
  audit that nothing else in the codebase reaches out to any other AI provider.
- The Gemini SDK (`google-genai`) is imported lazily inside `gemini_client.get_client()`,
  so dry-run mode works even before you `pip install` it or set an API key.
