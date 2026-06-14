-- Market-Research Scraper — Postgres schema
-- Run once against a dedicated DB:  CREATE DATABASE market_research;  \c market_research
-- Requires the pgvector extension (your postgres image already ships it).
--
-- Design goals:
--   * Capture ecommerce help/community content: questions, answers, comments, ratings, sections.
--   * Keep raw blobs in MinIO; store only structured rows + references here.
--   * Support BOTH retrieval modes: full-text (tsvector) and semantic (pgvector).
--   * Roll structured signals up into market-research insights (features, pain points, sentiment).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- 1. Sources & crawl orchestration
-- ---------------------------------------------------------------------------

-- A platform you point the scraper at (you provide the base URL).
CREATE TABLE sources (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,         -- e.g. 'shopify_help', 'triple_whale'
    name        text NOT NULL,                -- 'Shopify Help Center'
    base_url    text NOT NULL,                -- 'https://help.shopify.com'
    kind        text NOT NULL,                -- 'help_center' | 'community' | 'reviews' | 'docs'
    robots_ok   boolean DEFAULT true,         -- respect robots.txt / ToS (set per source)
    rate_limit_rpm int DEFAULT 20,            -- be polite: requests/minute
    enabled     boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- The crawl frontier / queue mirror (Redis holds the live queue; this is the durable log).
CREATE TABLE crawl_jobs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id   uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url         text NOT NULL,
    status      text NOT NULL DEFAULT 'queued', -- queued|fetching|done|error|skipped
    depth       int DEFAULT 0,
    error       text,
    queued_at   timestamptz DEFAULT now(),
    fetched_at  timestamptz,
    UNIQUE (source_id, url)
);

-- ---------------------------------------------------------------------------
-- 2. Raw pages (blob in MinIO, metadata here)
-- ---------------------------------------------------------------------------
CREATE TABLE pages (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url           text NOT NULL,
    title         text,
    section       text,                       -- breadcrumb / category, e.g. 'Payments > Payouts'
    http_status   int,
    content_hash  text,                        -- dedupe identical fetches
    minio_path    text,                        -- 's3://market-research/raw/shopify_help/<id>.html'
    fetched_at    timestamptz DEFAULT now(),
    UNIQUE (source_id, url, content_hash)
);

-- ---------------------------------------------------------------------------
-- 3. Structured discussion content (the heart of it)
-- ---------------------------------------------------------------------------

-- A thread = one question/topic/review unit on a page.
CREATE TABLE threads (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id    uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    page_id      uuid REFERENCES pages(id) ON DELETE SET NULL,
    external_id  text,                          -- platform's own id if present
    url          text,
    title        text,
    section      text,                          -- topic/category section
    author       text,
    is_answered  boolean,
    rating       numeric,                       -- stars (reviews) — nullable
    score        int,                           -- upvotes/likes/helpful count
    view_count   int,
    posted_at    timestamptz,
    tags         text[] DEFAULT '{}',
    created_at   timestamptz DEFAULT now(),
    UNIQUE (source_id, external_id)
);

-- A post = a unit inside a thread: the question body, an answer, or a comment/review.
CREATE TABLE posts (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id    uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role         text NOT NULL,                 -- 'question' | 'answer' | 'comment' | 'review'
    author       text,
    body         text NOT NULL,
    rating       numeric,                       -- per-review stars if applicable
    score        int,                           -- helpful/upvotes
    is_accepted  boolean DEFAULT false,         -- accepted answer / official reply
    posted_at    timestamptz,
    body_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body,''))) STORED,
    created_at   timestamptz DEFAULT now()
);
CREATE INDEX posts_body_tsv_idx ON posts USING gin (body_tsv);
CREATE INDEX posts_thread_idx   ON posts (thread_id);

-- ---------------------------------------------------------------------------
-- 4. Chunks for RAG (semantic + keyword). Embeddings filled via CLOUD API (no GPU).
-- ---------------------------------------------------------------------------
CREATE TABLE chunks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id      uuid REFERENCES posts(id) ON DELETE CASCADE,
    page_id      uuid REFERENCES pages(id) ON DELETE CASCADE,
    content      text NOT NULL,
    token_count  int,
    embedding    vector(1536),                  -- match EMBEDDING_DIM in .env
    content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(content,''))) STORED,
    metadata     jsonb DEFAULT '{}',
    created_at   timestamptz DEFAULT now()
);
CREATE INDEX chunks_embedding_idx ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX chunks_tsv_idx       ON chunks USING gin (content_tsv);

-- ---------------------------------------------------------------------------
-- 5. Market-research signal layer (the "why we scrape" payoff)
-- ---------------------------------------------------------------------------

-- Extracted signals: a pain point, feature request, complaint, praise, workaround...
CREATE TABLE signals (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     uuid REFERENCES sources(id) ON DELETE SET NULL,
    post_id       uuid REFERENCES posts(id) ON DELETE SET NULL,
    type          text NOT NULL,                -- 'pain_point'|'feature_request'|'complaint'|'praise'|'workaround'|'pricing'
    topic         text,                         -- normalized topic, e.g. 'checkout', 'shipping', 'analytics'
    summary       text NOT NULL,
    sentiment     numeric,                      -- -1.0 .. 1.0
    severity      int,                          -- 1..5 (how painful / how often it recurs)
    evidence_url  text,
    created_at    timestamptz DEFAULT now()
);
CREATE INDEX signals_topic_idx ON signals (topic);
CREATE INDEX signals_type_idx  ON signals (type);

-- Aggregated insights that drive the product decision (what to ship in ~3 months).
CREATE TABLE insights (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title         text NOT NULL,
    topic         text,
    quant_summary jsonb,                         -- counts, frequency, avg sentiment, top sources
    qual_summary  text,                          -- the narrative interpretation
    opportunity   text,                          -- product/feature implication
    confidence    numeric,
    created_at    timestamptz DEFAULT now()
);

-- Handy view: most common pain points by topic (quantitative starting point).
CREATE VIEW v_top_pain_points AS
SELECT topic,
       count(*)              AS mentions,
       round(avg(sentiment)::numeric, 2) AS avg_sentiment,
       round(avg(severity)::numeric, 2)  AS avg_severity
FROM signals
WHERE type IN ('pain_point','complaint','feature_request')
GROUP BY topic
ORDER BY mentions DESC;
