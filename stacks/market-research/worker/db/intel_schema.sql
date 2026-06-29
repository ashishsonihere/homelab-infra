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

-- SaaS product catalog dimension (TrustRadius, SaaSHub, AlternativeTo, etc.)
-- Reviews live in `documents` (source_slug='trustradius_review' etc.), linked via metadata.product_ext_id.
CREATE TABLE IF NOT EXISTS saas_products (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text NOT NULL,                  -- 'trustradius' | 'saashub' | 'alternativeto' | 'g2' | 'capterra'
    ext_id       text NOT NULL,                  -- product slug from the source
    name         text,
    vendor       text,
    category     text,
    website      text,
    pricing_text text,
    pricing      jsonb DEFAULT '{}',
    rating       numeric,
    n_reviews    int,
    description  text,
    metadata     jsonb DEFAULT '{}',
    scraped_at   timestamptz DEFAULT now(),
    UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS saas_products_source_idx ON saas_products (source);
CREATE INDEX IF NOT EXISTS saas_products_category_idx ON saas_products (category);
CREATE INDEX IF NOT EXISTS saas_products_rating_idx ON saas_products (rating DESC NULLS LAST);

-- Funded companies (capital-flow / wave-axis signal)
CREATE TABLE IF NOT EXISTS funded_companies (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    website    text,
    sector     text,
    stage      text,
    amount     numeric,
    investors  jsonb DEFAULT '[]',
    country    text,
    founded    int,
    source     text NOT NULL,
    ext_id     text NOT NULL,
    metadata   jsonb DEFAULT '{}',
    scraped_at timestamptz DEFAULT now(),
    UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS funded_companies_source_idx ON funded_companies (source);
CREATE INDEX IF NOT EXISTS funded_companies_sector_idx ON funded_companies (sector);

-- VC firms registry
CREATE TABLE IF NOT EXISTS vc_firms (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                 text NOT NULL,
    website              text,
    focus_sectors        jsonb DEFAULT '[]',
    stages               jsonb DEFAULT '[]',
    ticket_size          text,
    regions              jsonb DEFAULT '[]',
    portfolio_url        text,
    notable_investments  jsonb DEFAULT '[]',
    contact              jsonb DEFAULT '{}',
    source               text NOT NULL,
    ext_id               text NOT NULL,
    metadata             jsonb DEFAULT '{}',
    scraped_at           timestamptz DEFAULT now(),
    UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS vc_firms_source_idx ON vc_firms (source);
