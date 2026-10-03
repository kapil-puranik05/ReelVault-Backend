-- ReelVault schema for the backend2 (Gemini vision + audio) pipeline.
-- Run in the Supabase SQL editor after enabling pgvector. Reuses the SAME
-- Supabase project already used for auth — no new database account needed.
--
-- embedding vector(768) matches Gemini's gemini-embedding-001 model with
-- output_dimensionality=768 (see pipeline/config.py EMBEDDING_OUTPUT_DIM).

CREATE EXTENSION IF NOT EXISTS vector;

-- Core reels table — field names match pipeline.models.ReelRecord (snake_case),
-- which the API layer maps to the frontend's camelCase Reel shape on the way out.
CREATE TABLE IF NOT EXISTS reels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'youtube', 'tiktok', 'other')),
    title TEXT NOT NULL DEFAULT '',
    creator TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT NOT NULL DEFAULT '',
    video_url TEXT NOT NULL DEFAULT '',
    likes TEXT NOT NULL DEFAULT '0',
    views TEXT NOT NULL DEFAULT '0',
    duration_sec FLOAT NOT NULL DEFAULT 0,
    -- Array of { time: number, text: string } — matches the frontend's TranscriptItem[].
    transcript JSONB NOT NULL DEFAULT '[]'::jsonb,
    visual_summary TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Other'
        CHECK (category IN ('Tech', 'Food', 'Fitness', 'Finance', 'Productivity', 'Other')),
    action_type TEXT NOT NULL DEFAULT 'general'
        CHECK (action_type IN ('code', 'recipe', 'steps', 'general')),
    -- { code?, language?, ingredients?, steps?, extracted_content? }
    action_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags TEXT[] NOT NULL DEFAULT '{}',
    search_text TEXT NOT NULL DEFAULT '',
    embedding vector(768),
    processing_mode TEXT NOT NULL DEFAULT 'dry_run',
    cost_estimate_usd JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-frame visual context (from the vision pipeline) — optional, kept separate
-- from `reels` since the frontend never reads this directly; useful for future
-- "jump to timestamp" features or debugging vision quality.
CREATE TABLE IF NOT EXISTS reel_frames (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reel_id UUID NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    frame_index INT NOT NULL,
    timestamp_sec FLOAT NOT NULL,
    description TEXT,
    ocr_text TEXT,
    objects TEXT[],
    is_scene_change BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reels_user_id ON reels(user_id);
CREATE INDEX IF NOT EXISTS idx_reels_user_created ON reels(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reel_frames_reel_id ON reel_frames(reel_id);
CREATE INDEX IF NOT EXISTS idx_reels_embedding ON reels USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Row Level Security — each user can only ever see/modify their own reels,
-- even if a bug in the API layer forgot to filter by user_id.
ALTER TABLE reels ENABLE ROW LEVEL SECURITY;
ALTER TABLE reel_frames ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS reels_owner_all ON reels;
CREATE POLICY reels_owner_all ON reels
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS reel_frames_owner_all ON reel_frames;
CREATE POLICY reel_frames_owner_all ON reel_frames
    FOR ALL USING (EXISTS (SELECT 1 FROM reels r WHERE r.id = reel_id AND r.user_id = auth.uid()));

-- Semantic search function — the API layer calls this via `.rpc("search_reels", ...)`.
-- Note: the API server uses the Supabase SERVICE ROLE key (bypasses RLS), so it
-- passes match_user_id explicitly rather than relying on auth.uid() here.
CREATE OR REPLACE FUNCTION search_reels(
    query_embedding vector(768),
    match_user_id UUID,
    match_count INT DEFAULT 5
)
RETURNS TABLE (
    id UUID,
    source_url TEXT,
    platform TEXT,
    title TEXT,
    creator TEXT,
    thumbnail_url TEXT,
    video_url TEXT,
    likes TEXT,
    views TEXT,
    transcript JSONB,
    summary TEXT,
    category TEXT,
    action_type TEXT,
    action_data JSONB,
    tags TEXT[],
    created_at TIMESTAMPTZ,
    similarity FLOAT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        r.id, r.source_url, r.platform, r.title, r.creator, r.thumbnail_url,
        r.video_url, r.likes, r.views, r.transcript, r.summary, r.category,
        r.action_type, r.action_data, r.tags, r.created_at,
        1 - (r.embedding <=> query_embedding) AS similarity
    FROM reels r
    WHERE r.user_id = match_user_id
      AND r.embedding IS NOT NULL
    ORDER BY r.embedding <=> query_embedding
    LIMIT match_count;
$$;
