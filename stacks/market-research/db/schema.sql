-- Market-Research DB — Reddit-first, multi-source, RAG-ready.
-- Run:  CREATE DATABASE market_research;  \c market_research  then run this file.
-- Image pgvector/pgvector:pg16 → gen_random_uuid() is built-in (no uuid-ossp needed).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- 0. Source registry + crawl queue mirror
-- ---------------------------------------------------------------------------
CREATE TABLE sources (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug       text UNIQUE NOT NULL,          -- 'reddit', 'shopify_help', 'amazon'
    name       text NOT NULL,
    kind       text NOT NULL,                 -- 'community' | 'help_center' | 'reviews'
    base_url   text,
    enabled    boolean DEFAULT true,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE crawl_jobs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id  uuid REFERENCES sources(id) ON DELETE CASCADE,
    ref        text NOT NULL,                 -- post id / url / asin
    status     text NOT NULL DEFAULT 'queued',-- queued|fetching|done|error
    error      text,
    queued_at  timestamptz DEFAULT now(),
    done_at    timestamptz,
    UNIQUE (source_id, ref)
);

-- ---------------------------------------------------------------------------
-- 1. Reddit (priority 1) — posts + nested comment trees (recursive CTE)
-- ---------------------------------------------------------------------------
CREATE TABLE reddit_posts (
    id          bigserial PRIMARY KEY,
    reddit_id   varchar(15) UNIQUE NOT NULL,
    subreddit   text NOT NULL,
    title       text NOT NULL,
    selftext    text,
    author      text,
    score       int,
    upvote_ratio numeric,
    num_comments int,
    url         text,
    flair       text,
    created_utc bigint,
    fetched_at  timestamptz DEFAULT now()
);
CREATE INDEX reddit_posts_sub_idx ON reddit_posts (subreddit, created_utc);

CREATE TABLE reddit_comments (
    id               bigserial PRIMARY KEY,
    post_id          bigint REFERENCES reddit_posts(id) ON DELETE CASCADE,
    reddit_id        varchar(15) UNIQUE NOT NULL,
    parent_reddit_id varchar(15),             -- comment id (t1_) or post id (t3_), stripped
    body             text,
    author           text,
    score            int,
    depth            smallint,
    awards           int DEFAULT 0,
    created_utc      bigint,
    body_tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body,''))) STORED,
    fetched_at       timestamptz DEFAULT now()
);
CREATE INDEX reddit_comments_post_idx   ON reddit_comments (post_id, parent_reddit_id);
CREATE INDEX reddit_comments_tsv_idx    ON reddit_comments USING gin (body_tsv);
-- Rebuild a full thread:  WITH RECURSIVE t AS ( ...roots... UNION ALL ...children... ) SELECT * FROM t;

-- ---------------------------------------------------------------------------
-- 2. Shopify Help / community (priority 2)
-- ---------------------------------------------------------------------------
CREATE TABLE shopify_articles (
    id         bigserial PRIMARY KEY,
    url        text UNIQUE NOT NULL,
    title      text,
    section    text,
    body       text,
    tags       text[] DEFAULT '{}',
    updated_at timestamptz,
    fetched_at timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 3. Amazon (priority 3, limited)
-- ---------------------------------------------------------------------------
CREATE TABLE amazon_products (
    id          bigserial PRIMARY KEY,
    asin        varchar(12) UNIQUE NOT NULL,
    title       text,
    brand       text,
    price       numeric,
    rating      numeric,
    num_reviews int,
    url         text,
    fetched_at  timestamptz DEFAULT now()
);
CREATE TABLE amazon_reviews (
    id          bigserial PRIMARY KEY,
    product_id  bigint REFERENCES amazon_products(id) ON DELETE CASCADE,
    rating      int,
    title       text,
    body        text,
    helpful     int DEFAULT 0,
    verified    boolean,
    created_at  timestamptz,
    fetched_at  timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 4. Media (blobs in MinIO; refs here, link to any owner)
-- ---------------------------------------------------------------------------
CREATE TABLE media (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind  text NOT NULL,                -- 'reddit_post'|'reddit_comment'|'amazon_review'
    owner_id    bigint NOT NULL,
    src_url     text,
    minio_path  text,                         -- 'market-research/reddit/<id>.jpg'
    media_type  text,
    bytes       bigint,
    fetched_at  timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 5. Unified RAG layer — chunk any text, embed (local Ollama), hybrid search
-- ---------------------------------------------------------------------------
CREATE TABLE chunks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_slug text,                         -- 'reddit'|'shopify_help'|'amazon'
    owner_kind  text,                         -- which table the text came from
    owner_id    bigint,
    content     text NOT NULL,
    token_count int,
    embedding   vector(768),                  -- nomic-embed-text (Ollama) = 768 dims
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(content,''))) STORED,
    metadata    jsonb DEFAULT '{}',
    created_at  timestamptz DEFAULT now()
);
CREATE INDEX chunks_embedding_idx ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX chunks_tsv_idx       ON chunks USING gin (content_tsv);

-- ---------------------------------------------------------------------------
-- 6. Research output — signals → insights (the payoff)
-- ---------------------------------------------------------------------------
CREATE TABLE signals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_slug  text,
    owner_kind   text,
    owner_id     bigint,
    type         text NOT NULL,               -- pain_point|feature_request|complaint|praise|pricing|workaround
    topic        text,
    summary      text NOT NULL,
    sentiment    numeric,                     -- -1..1
    severity     int,                         -- 1..5
    evidence_url text,
    created_at   timestamptz DEFAULT now()
);
CREATE INDEX signals_topic_idx ON signals (topic);
CREATE INDEX signals_type_idx  ON signals (type);

CREATE TABLE insights (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title         text NOT NULL,
    topic         text,
    quant_summary jsonb,                       -- counts, freq, avg sentiment, top sources
    qual_summary  text,
    opportunity   text,                        -- product/feature implication (VC-fundable?)
    tech_viability text,
    confidence    numeric,
    created_at    timestamptz DEFAULT now()
);

-- Quant starting points
CREATE VIEW v_top_pain_points AS
SELECT topic, count(*) AS mentions,
       round(avg(sentiment)::numeric,2) AS avg_sentiment,
       round(avg(severity)::numeric,2)  AS avg_severity
FROM signals WHERE type IN ('pain_point','complaint','feature_request')
GROUP BY topic ORDER BY mentions DESC;

-- Seed the three sources
INSERT INTO sources (slug, name, kind, base_url) VALUES
 ('reddit','Reddit','community','https://www.reddit.com'),
 ('shopify_help','Shopify Help Center','help_center','https://help.shopify.com'),
 ('amazon','Amazon','reviews','https://www.amazon.com')
ON CONFLICT (slug) DO NOTHING;
