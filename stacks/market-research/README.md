# Market-Research Scraper

Scrapes ecommerce help/community/review sites you point it at (help.shopify.com, Triple Whale,
and any URL you provide), extracts **questions, answers, comments, ratings, and sections**,
structures them in Postgres, and rolls them up into **qualitative + quantitative market research**
to decide what product/features to ship.

Full design + data flow: see `homelab-vault/Projects/Market-Research-Scraper.md`.

## Pipeline (per source)

```
seed URL ──> Redis queue ──> worker ──> crawl4ai (fetch+clean)
                                  │
              ┌───────────────────┼───────────────────────────┐
              ▼                   ▼                           ▼
        MinIO (raw HTML)    Postgres (sources,           chunks (+ cloud
                            pages, threads, posts,        embeddings) → pgvector
                            signals, insights)            + Meilisearch (keyword)
```

## Bring-up order (mind the 4-core CPU)

1. **Create DB + schema** (on the server, against existing postgres):
   ```bash
   docker exec -it devcore-postgres psql -U admin -c "CREATE DATABASE market_research;"
   docker exec -i devcore-postgres psql -U admin -d market_research < db/schema.sql
   ```
2. **Set network names** in `docker-compose.yml` (`docker network ls` → fill `data_net` / `edge_net`).
3. **Secrets:** `cp .env.example secrets.env`, fill values, `sops --encrypt --in-place secrets.env`.
4. **Deploy crawler + worker first:** `bash ../../scripts/deploy.sh market-research`
   (comment out `meilisearch` until you actually need search — saves RAM/CPU).
5. Add a DNS record `search.lan → <devcore IP>` in Pi-hole when you enable Meilisearch.

## The worker (to build next)

`worker/` is a small Python app (Dockerfile + `main.py`). It: pops URLs from Redis, calls
crawl4ai, parses with selectors/Docling, writes structured rows, chunks text, calls the cloud
embedding API, upserts into Meilisearch. Scaffold this with Claude Code as step 2 of the build —
keep it polite: honor `robots.txt`, the per-source `rate_limit_rpm`, and each platform's ToS.
Prefer official APIs/exports where a platform offers them.
