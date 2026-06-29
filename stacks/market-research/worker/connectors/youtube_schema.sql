-- YouTube connector schema — CORRELATED channels→videos→comments + per-video LLM insights.
-- Mirrors the reddit_posts/reddit_comments correlated design: a natural-id UNIQUE on every table
-- so the connector can ON CONFLICT DO UPDATE (idempotent re-runs), and FK-by-natural-id
-- (channel_id / video_id text) so a streaming connector never needs the surrogate PK to correlate.
-- Run against the market_research DB (public schema):
--   psql "$PG_DSN" -f connectors/youtube_schema.sql
-- All statements are IDEMPOTENT (IF NOT EXISTS) so this is safe to re-apply on every deploy.

-- ---------------------------------------------------------------------------
-- 1. Channels — one row per YouTube channel (audited each ingest run)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS youtube_channels (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id       text UNIQUE NOT NULL,            -- natural id, e.g. 'UCxxxx'
    title            text,
    handle           text,                            -- '@handle'
    subscriber_count bigint,
    video_count      int,
    view_count       bigint,
    country          text,
    description      text,
    metadata         jsonb DEFAULT '{}',
    last_audited_at  timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 2. Videos — FK channel_id → youtube_channels.channel_id (natural id, like reddit link_id)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS youtube_videos (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id         text UNIQUE NOT NULL,            -- natural id, the 11-char watch id
    channel_id       text REFERENCES youtube_channels(channel_id),
    title            text,
    description      text,
    duration_sec     int,
    view_count       bigint,
    like_count       bigint,
    comment_count    bigint,
    published_at     timestamptz,
    tags             jsonb,
    transcript       jsonb,                           -- array of {text,start,dur}
    transcript_source text,                           -- 'youtube_transcript_api' | 'whisper' | NULL
    transcript_lang  text,                            -- BCP-47 lang code, e.g. 'en'
    raw              jsonb,                            -- full yt-dlp info dict (trimmed)
    fetched_at       timestamptz DEFAULT now(),
    updated_at       timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS youtube_videos_channel_idx ON youtube_videos (channel_id);
CREATE INDEX IF NOT EXISTS youtube_videos_published_idx ON youtube_videos (published_at);

-- ---------------------------------------------------------------------------
-- 3. Comments — correlated like reddit_comments: parent_id is a self-referential
--    natural-id tree (top-level comments have parent_id NULL; replies point at a comment_id).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS youtube_comments (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    comment_id   text UNIQUE NOT NULL,                -- natural id from yt-dlp
    video_id     text REFERENCES youtube_videos(video_id),
    parent_id    text,                                -- another comment_id, or NULL for top-level
    author       text,
    text         text,
    like_count   int,
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS youtube_comments_video_idx  ON youtube_comments (video_id);
CREATE INDEX IF NOT EXISTS youtube_comments_parent_idx ON youtube_comments (parent_id);
CREATE INDEX IF NOT EXISTS youtube_comments_published_idx ON youtube_comments (published_at);

-- ---------------------------------------------------------------------------
-- 4. Per-video LLM insights — one row per video (UNIQUE video_id → idempotent upsert).
--    Populated separately by connectors/youtube_analyze.py (mirrors analyze.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS youtube_video_insights (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id            text UNIQUE REFERENCES youtube_videos(video_id),
    scorecard_rating    int,                          -- 1..10
    summary             text,
    actionable_insights jsonb,
    pain_points         jsonb,
    ideas               jsonb,
    model               text,
    analyzed_at         timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS youtube_video_insights_video_idx ON youtube_video_insights (video_id);
