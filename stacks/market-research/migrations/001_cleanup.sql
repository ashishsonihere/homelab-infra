-- migrations/001_cleanup.sql — remove dead/legacy tables (TB-A)
-- Run: docker exec devcore-postgres psql -U devcore -d market_research -f migrations/001_cleanup.sql
--
-- VERIFIED ROW COUNTS (2026-06-29):
--   shopify_apps: 21,694 (redundant — identical to documents slug 'shopify_app')
--   signals: 9,069 (legacy analyze.py output, superseded by analysis.pain_signals)
--   chunks: 1,500 (abandoned pgvector/Meili embedding)
--   insights: 30 (legacy, superseded by analysis.opportunities)
--   amazon_products: 0 (never built)
--   amazon_reviews: 0 (never built)
--   search_jobs: 0 (never built)
--   shopify_articles: does not exist
--   entities, funding_rounds, crawl_jobs: verify below

-- SAFETY: verify counts before dropping. If any >0 table is not expected, STOP.
DO $$
DECLARE
  shopify_apps_count int;
  signals_count int;
  chunks_count int;
  insights_count int;
BEGIN
  SELECT count(*) INTO shopify_apps_count FROM shopify_apps;
  SELECT count(*) INTO signals_count FROM signals;
  SELECT count(*) INTO chunks_count FROM chunks;
  SELECT count(*) INTO insights_count FROM insights;
  
  RAISE NOTICE 'shopify_apps: %, signals: %, chunks: %, insights: %',
    shopify_apps_count, signals_count, chunks_count, insights_count;
END $$;

-- Drop the legacy view over signals
DROP VIEW IF EXISTS v_top_pain_points CASCADE;

-- Drop legacy/redundant tables (all verified 0 rows or superseded)
DROP TABLE IF EXISTS search_jobs CASCADE;
DROP TABLE IF EXISTS amazon_reviews CASCADE;
DROP TABLE IF EXISTS amazon_products CASCADE;
DROP TABLE IF EXISTS chunks CASCADE;
DROP TABLE IF EXISTS insights CASCADE;
DROP TABLE IF EXISTS signals CASCADE;
DROP TABLE IF EXISTS shopify_apps CASCADE;  -- redundant: data lives in documents (source_slug='shopify_app')
DROP TABLE IF EXISTS shopify_articles CASCADE;
DROP TABLE IF EXISTS crawl_jobs CASCADE;
DROP TABLE IF EXISTS funding_rounds CASCADE;
DROP TABLE IF EXISTS entities CASCADE;

-- Verify cleanup
DO $$
DECLARE
  remaining text;
BEGIN
  SELECT string_agg(tablename, ', ') INTO remaining
  FROM pg_tables
  WHERE schemaname = 'public'
    AND tablename IN ('search_jobs','amazon_reviews','amazon_products','chunks','insights','signals','shopify_apps','shopify_articles','crawl_jobs','funding_rounds','entities');
  IF remaining IS NOT NULL THEN
    RAISE EXCEPTION 'Tables still exist after cleanup: %', remaining;
  END IF;
  RAISE NOTICE 'Cleanup complete — all legacy tables dropped.';
END $$;
