# AGENTS.md — Market-Research Autonomous System

> Project memory shared by every agent harness (OpenCode reads this natively; `CLAUDE.md` and `GEMINI.md` symlink to it). Read this first, every session. Verbose context lives in the linked reference docs; this file is the operating guide.

## What this project is
A trustworthy **market-research data warehouse** on the `devcore` VM: it scrapes real **pain points** (Reddit, app-store reviews, forums, YouTube), **supply/competition** (SaaS directories, app stores), and **funding** signals, then runs a **5-tier analysis funnel** to surface a monetizable **SaaS wedge**. Goal: pick a wedge, validate it with real people, reach $10-20k MRR. Founder = Ashish (solo, ex-D2C operator, India cost base, cheap-model-first).

## Where things live
- **Git repo (on the LAPTOP):** `C:\Users\ashis\homelab` → this stack at `homelab-infra/stacks/market-research/`. Code of record; agents edit here. **`/opt/market-research/` on the server is a plain (non-git) deploy dir** synced manually (`scp`/`rsync`) — worker-code edits must be copied there to take effect (a git-based deploy is TB-F). **OpenCode runs on the laptop (no Docker here); for ALL Docker/Postgres/container ops the agent must use `ssh devcore "docker …"`.**
- **DB:** container `devcore-postgres`, superuser `devcore`, DB **`market_research`** (schemas `public` + `analysis`). Query: `docker exec devcore-postgres psql -U devcore -d market_research -c "..."`. App role `mr_worker`.
- **Secrets:** `/opt/market-research/worker.env` (mode 600 — `OPENROUTER_API_KEY`, `PG_DSN`, source keys). **NEVER commit.**
- **Reference:** [DATA-DICTIONARY.md](./DATA-DICTIONARY.md) (every DB/table/source, exact counts, KEEP/DROP) · the strategy vault is `homelab-vault/` (PRIVATE) — Master-Plan, Analysis-Architecture, Validation-Playbook, and the OpenCode task plan `homelab-vault/Projects/opencode-tasks/MASTER-PROMPT.md`.

## The stack
- **Ingest:** Python connectors (`httpx`, `curl_cffi`, `selectolax`, `yt-dlp`, `google-play-scraper`) run as Docker jobs. Reddit history via **Arctic Shift API** (free).
- **Store:** Postgres 16. **Analyze:** 5-tier funnel; LLM tiers call **OpenRouter** (DeepSeek-V3 bulk, frontier for judgment).
- **Images:** `mr-worker:scrape` (full: selectolax/curl_cffi/yt-dlp/sklearn — built by `Dockerfile.scrape`) · `mr-worker:lite` (httpx+psycopg2 — `Dockerfile.lite`, **build manually**, not in compose yet).
- **Ops:** full `devcore-*` platform stack (Traefik, Cloudflared, MinIO, Grafana/Prometheus/Loki, Metabase, Mathesar, n8n, Portainer, pgAdmin, Uptime-Kuma, postgres-backup).

## How to run things
```bash
# Analysis (one-shot job, replicates the canonical launch):
docker run --rm --network devcore_net --memory=3g --env-file /opt/market-research/worker.env \
  -e AN_TOP_N=20000 -e AN_BATCH=200 mr-worker:scrape python3 -m analysis.tier3_extract
docker run --rm --network devcore_net --memory=3g --env-file /opt/market-research/worker.env \
  -e AN_TIER4_TOP_N=50 mr-worker:scrape python3 -m analysis.tier4_validate
# Tier-0 lexical filter → pain_signals:
docker run --rm --network devcore_net --env-file /opt/market-research/worker.env mr-worker:scrape python3 -m analysis.tier0_filter
# Scrapers: run_feeds.sh (lite: hackernews,news_rss,ycombinator,stackexchange,producthunt,appstore,wordpress)
#           run_scrape.sh (scrape: google_play,appsumo) ;  bd_run.sh (Bright Data)
# Reddit backfill shard: docker run mr-worker:scrape python -m connectors.arctic_reddit  (env AS_SUBREDDITS,AS_AFTER,AS_BEFORE,AS_RESUME=1)
```
- **Crons (root crontab):** `cron_reddit_daily.sh` (3:30am, new posts) · `cron_reddit_gapscan.sh` (Sun 5:30am) · `restart_backfill.sh` (@reboot). Logs in `/var/log/mr_*`.
- **Live containers (canonical):** `mr-reddit-{e,d,fix}` (3 backfill shards), `mr-reddit-daily`, `mr-ollama`, `mr-metabase`, + the `devcore-*` platform. Job containers should be `--rm`.

## The 5-tier funnel (state)
`Tier-0` SQL lexical filter → **`analysis.pain_signals` (10.3M, DONE)** → `Tier-1` embeddings (ABANDONED, CPU-infeasible) → `Tier-2` cluster (SKIPPED) → `Tier-3` `tier3_extract.py` DeepSeek → **`problem_statements`** → `Tier-4` `tier4_validate.py` 3-axis score → **`opportunities`**. As of 2026-06-29: statements=15, opportunities=1 (proof only; scaled run in progress).

## The scoring framework (enforced by the schema)
`problem_statements` columns = `icp, job_to_be_done, current_workaround, wtp_quotes, severity, frequency_note`. `opportunities` = `wedge_score (.5) + wave_score (.3) + edge_score (.2)`, `saturation_ok` hard-gate (≥3 funded competitors → final_score 0), `competitors`, `expansion_vector`. **Selection rule:** wedge-first + a named expansion vector. **Waves:** AI-native ops / vertical AI agents / data sovereignty. **Edge:** ecom/D2C operator + AI-native builder + India cost base. Any agent writing these tables must fill these fields — the Lean-Canvas/WTP/severity framework is structural, not optional.

## Conventions & RULES
1. **Server-first.** Code lands in the repo + `/opt/market-research`, committed. 2. **One task = one branch = one PR** (git worktree; never collide). 3. **Test before commit; never push secrets** (`worker.env`, `*.env`, keys) — verify `git status` first. 4. **Cheap models for bulk** (DeepSeek-V3/GLM-5.2), frontier only for hardest judgment (Tier-4, hard reviews). 5. **Cloudflare/anti-bot block → report `CF_BLOCKED` and stop**, don't loop (G2/Capterra/GetApp are hard-blocked). 6. **Verify before you destroy** — `count(*)` / not-referenced before any `DROP`; if reality contradicts the docs, STOP and report. 7. **The DB has never been `ANALYZE`d** — trust `count(*)`, not planner estimates; `VACUUM ANALYZE` is step 1 of cleanup. 8. **Update this file + `[[opinions]]` at session end** (enforced by the `Stop` hook + the `/remember` skill).

## Known issues / gaps (don't re-discover these)
- Tier-3 `upsert_statement` has a cosmetic `ON CONFLICT (cluster_id) WHERE cluster_id IS NULL` that is **harmless** (NULLs are distinct → no cap; the 15 statements were a tiny proof run, not a bug). Clean it to a plain `INSERT … RETURNING id`.
- Tier-4 does **no web validation** despite its docstring (`AN_TIER4_WEB_SEARCH` unused) — scores are LLM-only. Implement real web evidence for v2.
- **Schema-as-code gaps:** `funded_companies`/`vc_firms` exist live with no DDL in repo; 3 drift columns not in committed DDL (`reddit_comments.parent_id`, `pain_signals.deduped`, `problem_clusters.cluster_key`). Capture before any fresh deploy.
- `homelab-vault/Scraper-Registry.md` is stale — DATA-DICTIONARY.md supersedes it.
