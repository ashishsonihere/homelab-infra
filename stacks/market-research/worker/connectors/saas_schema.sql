-- SaaS review/products schema — structured dimension table for product catalogs across
-- TrustRadius, SaaSHub, FeaturedCustomers, (G2/Capterra when CF bypass is available).
-- Reviews go into the existing `documents` table (source_slug='trustradius_review' etc.)
-- linked back to products via metadata.product_ext_id — mirrors appstore_reviews pattern.

CREATE TABLE IF NOT EXISTS saas_products (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text NOT NULL,              -- 'trustradius' | 'saashub' | 'featuredcustomers' | 'g2' | 'capterra'
    ext_id       text NOT NULL,              -- product slug from the source
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

-- Ensure documents table has indexes for the new review source_slugs
-- (documents table is created by db/intel_schema.sql; these indexes are additive)
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source_slug);
