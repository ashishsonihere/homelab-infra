# homelab-infra

Self-hosted market-research data warehouse + 5-tier analysis pipeline that scrapes real customer pain points (Reddit, app-store reviews, forums, YouTube), supply/competition signals (SaaS directories, app stores, funding data), and runs a funnel from 10M+ raw signals to scored SaaS opportunities.

## What's here

```
homelab-infra/
  stacks/
    market-research/        # the main project
      worker/               # Python connectors + analysis pipeline
        connectors/         # 20+ source scrapers (Reddit, app stores, SaaS dirs, YouTube, funding)
        analysis/           # 5-tier funnel (Tier-0 lexical → Tier-3 LLM → Tier-4 scoring)
        tests/              # 129 unit tests (FakeCursor idempotency, LLM mock, CF block handling)
        db/                 # schema DDL
      docker-compose.yml    # profiles: always-on (shards), oneshot (connectors), analysis (tier3/4)
      AGENTS.md             # project memory (read by all AI agents)
      DATA-DICTIONARY.md    # every table, exact counts, KEEP/DROP
      migrations/           # SQL migrations
  .github/workflows/        # CI (ruff lint + format + pytest)
```

## The pipeline

1. **Ingest** — Python connectors scrape 18+ sources into Postgres: Reddit (28M rows via Arctic Shift API), app-store reviews (App Store, Google Play, Shopify), SaaS directories (TrustRadius, SaaSHub, AlternativeTo), YouTube transcripts, funding data (YC, Crunchbase). All use `curl_cffi` for TLS fingerprinting, residential proxies for anti-bot bypass, and idempotent `ON CONFLICT` upserts.

2. **Store** — Postgres 16 with schemas `public` (raw data + dimensions) and `analysis` (the funnel).

3. **Analyze** — A 5-tier funnel:
   - **Tier-0**: SQL lexical filter → 10.3M `pain_signals` (lexicon hits, question-shape, engagement)
   - **Tier-1/2**: Embeddings + clustering (ABANDONED — CPU-infeasible on 4-core box)
   - **Tier-3**: DeepSeek-V3 groups top pain signals into `problem_statements` (Pydantic-validated, ~$0.12/run)
   - **Tier-4**: DeepSeek validates statements → `opportunities` with 3-axis score (wedge .5 / wave .3 / edge .2, saturation hard-gate)

4. **Score** — `final_score = 0.5*wedge + 0.3*wave + 0.2*edge`; `saturation_ok=false → final_score=0` (hard gate if 3+ funded incumbents on exact ICP).

## Tech stack

- **Postgres 16** (pgvector for embeddings, halfvec for Matryoshka)
- **Python 3.12** (httpx, curl_cffi, selectolax, yt-dlp, pydantic, psycopg2)
- **Docker** (compose with profiles: always-on, oneshot, analysis)
- **OpenRouter** (DeepSeek-V3 for bulk LLM, Claude Sonnet for judgment)
- **129 unit tests** (FakeCursor idempotency, LLM mock, Cloudflare block handling)

## Quickstart

```bash
# Run the analysis funnel
docker compose --profile analysis run --rm mr-tier3
docker compose --profile analysis run --rm mr-tier4

# Run a scraper
docker compose --profile oneshot run --rm mr-worker-scrape python -m connectors.saas_reviews

# Run tests
cd stacks/market-research/worker
pip install -r requirements.txt pytest
pytest -q --ignore=tests/test_analysis.py
```

## Status

- 10.3M pain signals filtered and scored
- 28M Reddit rows (posts + comments) across 89 subreddits
- 278K documents from 18 sources
- 5,991 funded companies + 66 VC firms
- 129 unit tests, all passing
- CI: ruff lint + format + pytest
