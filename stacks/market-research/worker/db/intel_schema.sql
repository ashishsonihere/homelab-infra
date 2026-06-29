-- Tier-1 intelligence: generic, source-agnostic tables. Run against the market_research DB.
-- Complements the existing sources/signals/chunks/reddit/amazon tables (schema.sql).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_slug    text NOT NULL,                 -- 'hackernews','ycombinator','producthunt',...
  ext_id         text,                          -- source's own id
  url            text,
  title          text,
  body           text,
  raw_minio_path text,                          -- raw blob in MinIO if large
  content_hash   text,
  metadata       jsonb DEFAULT '{}',
  fetched_at     timestamptz DEFAULT now(),
  UNIQUE (source_slug, ext_id)
);
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents(source_slug);
CREATE INDEX IF NOT EXISTS documents_tsv_idx ON documents USING gin (to_tsvector('english', coalesce(title,'')||' '||coalesce(body,'')));

CREATE TABLE IF NOT EXISTS entities (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind       text NOT NULL,                     -- company|app|product|person|vc
  name       text NOT NULL,
  url        text,
  metadata   jsonb DEFAULT '{}',
  created_at timestamptz DEFAULT now(),
  UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS funding_rounds (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid REFERENCES entities(id) ON DELETE CASCADE,
  amount       numeric, currency text, stage text,
  investors    text[], announced_on date,
  source_slug  text, evidence_url text
);

CREATE TABLE IF NOT EXISTS shopify_apps (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text, category text, url text UNIQUE,
  launched_at   date, review_count int, rating numeric,
  merchant_urls text[], fetched_at timestamptz DEFAULT now()
);
