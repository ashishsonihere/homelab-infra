# Market-Research System — Data Dictionary & State of the System
_Verified live against `devcore` on 2026-06-29. All counts are exact `count(*)`, not planner estimates. This file is the single source of truth for what exists and why; AGENTS.md links here._

> ⚠️ **Finding:** the `market_research` DB has **never been `ANALYZE`d** (`last_analyze = never` on every table). So Postgres planner estimates (`n_live_tup`, `reltuples`) are wrong — e.g. `documents` reports `829` but truly holds `278,978`. **Always trust `count(*)`.** First maintenance step for cleanup: `VACUUM ANALYZE` the whole DB.

---

## 1. The 5 databases on `devcore-postgres` — which are OURS

| DB | Owner | Ours? | What it is |
|---|---|---|---|
| **`market_research`** | us | ✅ **THE project** | All scraped data + the analysis funnel (schemas `public` + `analysis`) |
| `mathesar` | Mathesar app | ❌ app-internal | The spreadsheet-UI's own Django metadata (`mathesar_*`, `auth_*`, `django_*`). Do not touch |
| `n8n` | n8n app | ❌ app-internal | The automation tool's workflow/execution store. Barely used (1-2 rows). Do not touch |
| `homelab_inventory` | a *separate* project | ❌ not this project | Homelab infra inventory (`hosts, containers, networks, services, ports`) — all 0 rows, seeded but unused. This is the "Eniten/entity" DB you saw; it is **not** market-research |
| `postgres` | system | ❌ | Default admin DB, empty |

**Why `msar.*` and `mathesar_types` show up in every DB:** Mathesar installs helper schemas into every database it manages — harmless. **Why `pg_temp_*.reddit_stage_c` (100k-800k rows) appear:** those are the Reddit shards' *temporary* staging tables during backfill — transient, vanish when a shard finishes. Normal.

---

## 2. `market_research` → schema `public` (raw data + dimensions)

| Table | Rows (exact) | Where the data came from | Purpose / correlation | Verdict |
|---|---|---|---|---|
| `reddit_comments` | **22,346,697** ▲ | `arctic_reddit.py` (Arctic Shift archive API) | Largest pain corpus. FK `post_id`→`reddit_posts`, tree via `parent_id`. Feeds Tier-0 | ✅ KEEP |
| `reddit_posts` | **6,483,443** ▲ | same | Submissions; `(subreddit, created_utc)` indexed. Feeds Tier-0 | ✅ KEEP |
| `documents` | **278,978** | 14 connectors (app stores, forums, SaaS dirs…) | **Universal text store**; 18 `source_slug`s. The non-Reddit pain corpus. Feeds Tier-0 | ✅ KEEP |
| `shopify_apps` | 21,694 | `shopify_appstore.py` (older path) | **REDUNDANT** — identical to `documents` slug `shopify_app` (21,694). Same data twice | 🪦 DROP (after confirming `documents` is canonical) |
| `signals` | 9,069 | `analyze.py` (legacy pre-funnel) | Old per-document signal rows; superseded by `analysis.pain_signals` | 🪦 DROP (+ view `v_top_pain_points`) |
| `funded_companies` | 5,991 | loaded out-of-band (**no connector, no DDL in repo**) | Capital-flow / "wave" signal + future fundraising-outreach list. Referenced by Tier-4 (FK not wired) | ✅ KEEP ⚠ capture DDL |
| `chunks` | 1,500 | `embed.py` (abandoned pgvector/Meili) | Leftover embedding chunks; approach abandoned | 🪦 DROP |
| `youtube_videos` | 731 (686 transcribed) | `youtube.py` (yt-dlp) | Video metadata + transcripts (jsonb). Feeds Tier-0 + `youtube_analyze` | ✅ KEEP |
| `saas_products` | 511 | `saas_reviews.py` (TrustRadius) | SaaS catalog dimension; reviews live in `documents` | 🗂 KEEP |
| `youtube_channels` | 494 | `youtube.py` | Channel dimension (all 494 have videos) | 🗂 KEEP |
| `sources` | 388 | seed `sources_seed.sql` | Source catalog/ambitions (see §4). FK'd by `crawl_jobs` | 🗂 KEEP |
| `vc_firms` | 66 | out-of-band (**no DDL in repo**) | VC registry; same purpose as `funded_companies` | ✅ KEEP ⚠ capture DDL |
| `insights` | 30 | `analyze.py` (legacy) | Old rolled-up opportunities; superseded by `analysis.opportunities` | 🪦 DROP |
| `youtube_comments` | 27 | `youtube.py` | Comment trees; barely populated | ✅ KEEP |
| `youtube_video_insights` | **0** | `youtube_analyze.py` (never run) | Per-video LLM scorecard sink — empty because the analyzer hasn't run | 💤 KEEP (staged) |
| `search_jobs`, `amazon_products`, `amazon_reviews` | 0 | — | Never built (no connector) | 🪦 DROP |

## 3. `market_research` → schema `analysis` (the 5-tier funnel)

| Table | Rows | Stage → produced by | Purpose | Verdict |
|---|---|---|---|---|
| `pain_signals` | **10,313,742** | **Tier-0** `tier0_filter.py` (SQL lexical filter) | The deduped, scored unit of pain. The pipeline's core working table | ✅ KEEP |
| `problem_clusters` | 0 | Tier-2 (SKIPPED — embeddings infeasible on CPU) | Cluster sink; staged for later | 💤 KEEP |
| `problem_statements` | **15** | **Tier-3** `tier3_extract.py` (DeepSeek) | LLM-grouped problem themes. **Only 15 = a tiny proof run, NOT a bug** | ✅ KEEP |
| `opportunities` | **1** | **Tier-4** `tier4_validate.py` (DeepSeek) | 3-axis scored (wedge .5 / wave .3 / edge .2, saturation gate). The payoff table | ✅ KEEP |
| `opportunity_competitors` | 4 | Tier-4 | Competitor sidecar per opportunity | ✅ KEEP |

**The framework lives in the schema.** `problem_statements` columns = `icp, job_to_be_done, current_workaround, wtp_quotes, severity, frequency_note` (Lean-Canvas / WTP / severity). `opportunities` = `wedge_score, wave_score, edge_score, saturation_ok, final_score, competitors, expansion_vector`. Any model that writes these tables is *forced* to fill the framework fields → the rubric is enforced by structure, not prompt discipline.

---

## 4. Source coverage — what's built vs the 388-source catalog

The `sources` table is the **ambition list (388)**, not what's done. Status: **`planned` 371**, `paid` 6, `needs-key` 5, **`live` 4**, `needs-proxy` 2. Tracks: **A (demand/pain) 199**, **B (supply/trade/competition) 187**.

**Actually producing data today (18 connectors → `documents` + reddit + youtube):**
appstore_reviews 135,904 · google_play_reviews 87,924 · shopify_app 21,694 · wordpress_plugins 10,180 · reddit(docs) 6,194 · atlassian_app 6,000 · ycombinator 5,965 · devto 1,430 · trustradius_review 804 · appsumo 708 · producthunt 589 · hackernews 574 · chrome_extension 437 · stackexchange 417 · news_rss 133 · shopify_review 16 · alternativeto 6 · saashub_review 3 — **plus** Reddit (28.8M rows) and YouTube (731 videos).

**Built but blocked / abandoned:** G2 / Capterra / GetApp / SoftwareAdvice (Cloudflare Turnstile — hard-blocked, `CF_BLOCKED`); `alternativeto` (6 rows, brittle).
**Catalogued but not built (the bulk of "300+ sources"):** the 371 `planned` rows — Track-B trade data (UN Comtrade, US Census, Eurostat), Amazon/Flipkart, funding/VC dirs (Tracxn, Crunchbase, Tiger Global…), etc. These are *future work*, not running scrapers.

---

## 5. Reddit subreddit coverage (live)

**89 subreddits have data.** **Posts are essentially complete** — almost every sub's newest post is 2026-06-2x and oldest is 2020-01 (or the sub's birth date). The Reddit shards are now in the **comment-backfill phase** (comments lag posts; that's what the `[sub/comments] N up to DATE` logs mean).

**Backfill assignment (3 live shards, 57 sub-slots; many already done by earlier runs):**
- `mr-reddit-e` (23 subs): buildinpublic, business, freelance, venturecapital, ycombinator, selfhosted, ExperiencedDevs, dataanalysis, BusinessIntelligence, marketingagency, digitalagency, dropshippingindia, IndianStreetBets, beermoneyindia, 3dprintIndia, …
- `mr-reddit-d` (22 subs): AI_Agents, AgentsOfAI, aiagents, LocalLLaMA, ArtificialInteligence, AmazonFBATips, AmazonVine, AmazonSellerUS, FBA, PPCMarketing, programmatic, b2bmarketing, leadgeneration, SaaSMarketing, agencylife, …
- `mr-reddit-fix` (12 subs): Entrepreneur, microsaas, ecommercemarketing, agency, indiehackers, FacebookAds, smallbusinessindia, shopifyDev, B2BSaaS, TikTokMarketing, cro, AmazonFBAOnlineRetail

**Needs attention (incomplete):** `OnlineBusiness` (stuck — newest only 2021-06-18) · `DigitalMarketingIndia` (302 posts, oldest 2025-12) · a few thin subs (`Buildwithreddit` 503, `Magento` 3,977).

**ETA:** Posts = done. Comments (22.3M, climbing) are the long pole — full backfill to 2020 across all subs is realistically a few more days at current pace, **but the corpus is already analysis-ready** (high-engagement recent comments, the densest pain, are in). Analysis does not need 100% comment depth to start.

---

## 6. YouTube status (answer: it's "complete for what was seeded", insights NOT run)
- **731 videos** scraped, **686 (94%) have transcripts**; **494 channels**, all with videos. This is the **seeded set** — whatever channel/video seed list was used, not your full watch-later. If you expected more, the seed simply didn't include them; ingest of the seed is done.
- **`youtube_video_insights = 0`** — the LLM analyzer (`youtube_analyze.py`) has **never run**, so there is nothing to look at yet. Generating insights is a pending task (cheap: ~$ for 731 videos via DeepSeek/gpt-4o-mini).
- `youtube_comments = 27` — comment ingest barely started (low priority; transcripts are the gold).

---

## 7. Ops — crons, shards, where the files live
- **Cron (root crontab on `devcore`):**
  - `30 3 * * *` → `/opt/market-research/cron_reddit_daily.sh` (daily new-posts top-up) → `/var/log/mr_reddit_daily.log`
  - `30 5 * * 0` → `/opt/market-research/cron_reddit_gapscan.sh` (weekly gap re-scan) → `/var/log/mr_reddit_gapscan.log`
  - `@reboot` → `/opt/market-research/restart_backfill.sh` (relaunches shards after reboot)
- **Daily cron is ACTIVE** via the `mr-reddit-daily` container (Up). It tops up the same subs with new posts.
- **Backfill scrapers (run scripts):** `run_feeds.sh` (lite image: hackernews, news_rss, ycombinator, stackexchange, producthunt, appstore, wordpress), `run_scrape.sh` (scrape image: google_play, appsumo), `bd_run.sh` (Bright Data). All under `/opt/market-research/`.
- **Token spend so far ≈ nil.** Scrapers are free (no LLM). The only LLM spend is the Tier-3/4 proof (DeepSeek, ~$0.13) and `youtube_analyze` hasn't run (0 rows). There is **no token waste** to date — the cost is all ahead of us (the scaled Tier-3/4 run ≈ $1-2).

---

## 8. Stragglers to remove (the cleanup target)
`shopify_apps` (redundant) · `signals` + view `v_top_pain_points` (legacy) · `chunks` (abandoned) · `insights` (legacy) · `search_jobs`, `amazon_products`, `amazon_reviews` (never built) · likely-empty `shopify_articles, media, crawl_jobs, entities, funding_rounds, seller_reports` (**verify 0 rows first**). Legacy CODE: `reddit_feed.py, reddit_scraper.py, embed.py, index_meili.py, analyze.py, reddit_worker.py + root Dockerfile`. **Before dropping anything:** `VACUUM ANALYZE`, capture `funded_companies`/`vc_firms` DDL into the repo, and add 3 drift columns to committed DDL (`reddit_comments.parent_id`, `pain_signals.deduped`, `problem_clusters.cluster_key`).

---

## 9. Doc-accuracy flags
- `homelab-vault/Scraper-Registry.md` is **stale** (says "6 shards / 43 subs / 440k posts") — reality is 3 shards, 89 subs, 6.48M posts. Update it.
- Counts in older vault notes predate the current backfill — this file supersedes them.
