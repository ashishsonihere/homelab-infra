# AGENTS.md — Market-Research Autonomous System

> Project memory shared by every agent harness. OpenCode reads this natively; `CLAUDE.md` and `GEMINI.md` are synced copies of this file (kept in sync by `scripts/sync-memory.sh` / the pre-commit hook — Windows can't symlink reliably). **Read this first, every session.** Verbose context lives in the linked reference docs; this file is the operating guide. Before any judgment call (wedge selection, scoring, outreach), also read **[[opinions]]** — the founder's beliefs override the internet-average answer.

## What this project is
A trustworthy **market-research data warehouse** on the `devcore` VM: it scrapes real **pain points** (Reddit, app-store reviews, forums, YouTube), **supply/competition** (SaaS directories, app stores), and **funding** signals, then runs a **5-tier analysis funnel** to surface a monetizable **SaaS wedge**. Goal: pick a wedge, validate it with real people, reach $10-20k MRR in 3-4 months. Founder = Ashish (solo, ex-D2C operator, India cost base, cheap-model-first).

## Where things live
- **Git repo (on the LAPTOP):** `C:\Users\ashis\homelab` → this stack at `homelab-infra/stacks/market-research/`. Code of record; agents edit here. **`/opt/market-research/` on the server is a plain (non-git) deploy dir** synced manually (`scp`/`rsync`) — worker-code edits must be copied there to take effect (a git-based deploy is TB-F). **OpenCode runs on the laptop (no Docker here); for ALL Docker/Postgres/container ops the agent must use `ssh devcore "docker …"`.**
- **DB:** container `devcore-postgres`, superuser `devcore`, DB **`market_research`** (schemas `public` + `analysis`). Query: `docker exec devcore-postgres psql -U devcore -d market_research -c "..."`. App role `mr_worker` (least-priv, currently grants `ALL` on `public` — tighten in TB-K).
- **Secrets:** `/opt/market-research/worker.env` (mode 600 — `OPENROUTER_API_KEY`, `PG_DSN`, source keys). **NEVER commit.** Verify `git status` shows none before any push.
- **Reference docs:** [DATA-DICTIONARY.md](./DATA-DICTIONARY.md) (every DB/table/source, exact counts, KEEP/DROP) · the strategy vault is `homelab-vault/` (PRIVATE) — Master-Plan, Analysis-Architecture, Validation-Playbook, and the OpenCode task plan `homelab-vault/Projects/opencode-tasks/MASTER-PROMPT.md`. The founder's judgment/taste lives in **[[opinions]]** (`homelab-vault/opinions.md`).

## Repo layout
```
stacks/market-research/
├── AGENTS.md              # this file — project memory (source of truth for CLAUDE.md / GEMINI.md)
├── CLAUDE.md              # synced copy of AGENTS.md (Claude Code harness)
├── GEMINI.md              # synced copy of AGENTS.md (Antigravity/Gemini harness)
├── DATA-DICTIONARY.md     # every table/source, exact counts, KEEP/DROP — read before any DB op
├── README.md              # public-facing portfolio README
├── docker-compose.yml     # 3 services (worker[jobs], ollama, metabase); ad-hoc `docker run` sprawl → TB-C
├── db/
│   └── schema.sql         # committed schema (has drift — see Known issues; TB-A rescues)
├── .env.example           # template for worker.env (never commit the real one)
├── run_feeds.sh           # cron runner — lite-image connectors (7 free API/RSS feeds)
├── run_scrape.sh          # cron runner — scrape-image connectors (google_play, appsumo)
├── bd_run.sh              # Bright Data runner (trigger backfill | trigger daily | collect)
├── brightdata_jobs*.json  # BD job configs (brightdata_jobs.example.json is the template)
├── setup_role.sh          # creates the `mr_worker` least-priv DB role
├── reports/
│   └── opportunities-v1.md  # Tier-3/4 output report (top opportunities by final_score)
├── scripts/
│   ├── remember.sh        # session-end hook — appends learnings to AGENTS.md (TB-D)
│   └── sync-memory.sh    # re-copies AGENTS.md → CLAUDE.md + GEMINI.md (pre-commit)
└── worker/                # Python code (the actual product)
    ├── requirements.txt
    ├── Dockerfile         # LEGACY (PRAW+RQ) — dead, do not use
    ├── Dockerfile.lite    # → mr-worker:lite  (httpx+psycopg2) — build manually (TB-C wires into compose)
    ├── Dockerfile.scrape  # → mr-worker:scrape (selectolax, curl_cffi, yt-dlp, sklearn) — the real image
    ├── connectors/        # scrapers — one module per source (see "How to run each connector")
    ├── analysis/          # the 5-tier funnel — tier0_filter, tier3_extract, tier4_validate (+ abandoned tier1/tier2)
    ├── db/                # intel_schema.sql (funded_companies/vc_firms — was missing, TB-A rescues) + sources_seed.sql
    ├── seeds/             # static seed data (youtube channel CSVs, video IDs)
    ├── tests/             # pytest (conftest, fixtures, stress_youtube + a few unit tests — TB-I expands)
    ├── analyze.py         # LEGACY (documents→signals→insights) — superseded by analysis/*
    ├── embed.py           # LEGACY (pgvector/Meili, abandoned)
    ├── index_meili.py     # LEGACY (Meili, abandoned)
    ├── ingest_sellersprite.py  # one-off ingest, not wired to a cron
    └── reddit_worker.py   # LEGACY (old PRAW+RQ worker) — superseded by connectors.arctic_reddit
```

## The stack
- **Ingest:** Python connectors (`httpx`, `curl_cffi` for TLS/JA3, `selectolax`, `yt-dlp`, `google-play-scraper`) run as Docker jobs. Reddit history via **Arctic Shift API** (free).
- **Store:** Postgres 16 (`market_research`, schemas `public` + `analysis`). Redis present (legacy RQ, retiring).
- **Analyze:** 5-tier funnel; LLM tiers call **OpenRouter** (DeepSeek-V3 bulk, Claude-Sonnet only as judge label). Ollama present for local embeddings (abandoned on CPU).
- **Images:** `mr-worker:scrape` (full — `Dockerfile.scrape`) · `mr-worker:lite` (httpx+psycopg2 — `Dockerfile.lite`, **build manually**, not in compose yet). Legacy root `Dockerfile` (PRAW+RQ) is dead.
- **Ops:** full `devcore-*` platform stack (Traefik, Cloudflared, MinIO, Grafana/Prometheus/Loki, Metabase, Mathesar, n8n, Portainer, pgAdmin, Uptime-Kuma, postgres-backup).

## How to run each connector
All commands run **on the server** via `ssh devcore "..."` (the laptop has no Docker). `ENVF=/opt/market-research/worker.env`. Module pattern: `python -m connectors.<name>`.

**Lite-image connectors** (httpx+psycopg2 — free APIs/RSS, run by `run_feeds.sh`):
```bash
docker run --rm --network devcore_net --env-file "$ENVF" mr-worker:lite python -m connectors.<name>
# <name> = hackernews | news_rss | ycombinator | stackexchange | producthunt | appstore | wordpress
```
**Scrape-image connectors** (selectolax/curl_cffi — need TLS fingerprinting, run by `run_scrape.sh`):
```bash
docker run --rm --network devcore_net -v /opt/market-research/worker:/app -v /opt/market-research:/mr \
  -w /app --env-file "$ENVF" mr-worker:scrape python -m connectors.<name>
# <name> = google_play | appsumo | shopify_appstore | atlassian_marketplace | chrome_webstore | devto | youtube | saas_reviews | youtube_analyze
```
**Bright Data** (paid unlocker, cost-gated — `bd_run.sh`):
```bash
bd_run.sh trigger backfill   # historical backfill jobs
bd_run.sh trigger daily       # daily incremental (cron)
bd_run.sh collect             # download + ingest ready snapshots (hourly)
```
**Arctic Shift Reddit backfill** (free, the 3 live shards — env-driven, resumable):
```bash
docker run --rm --network devcore_net --env-file "$ENVF" \
  -e AS_SUBREDDITS=shopify,ecommerce -e AS_AFTER=2020-01-01 -e AS_BEFORE=2026-06-29 -e AS_RESUME=1 \
  mr-worker:scrape python -m connectors.arctic_reddit
```
**Anti-bot reality:** G2 / Capterra / GetApp / SoftwareAdvice = hard-blocked (Cloudflare Turnstile, even residential) → do not retry; report `CF_BLOCKED` and stop. TrustRadius / SaaSHub (brittle) / AlternativeTo = accessible via curl_cffi. `alternativeto.py` excluded from run scripts (6 rows, brittle).

**Analysis tiers** (`worker/analysis/`, run as one-shot `--rm` jobs):
```bash
# Tier-0 — SQL lexical filter → analysis.pain_signals (DONE, 10.3M)
docker run --rm --network devcore_net --env-file "$ENVF" mr-worker:scrape python3 -m analysis.tier0_filter
# Tier-3 — DeepSeek groups top pain_signals into problem_statements
docker run --rm --network devcore_net --memory=3g --env-file "$ENVF" \
  -e AN_TOP_N=20000 -e AN_BATCH=200 -e AN_TIER3_MODEL=deepseek-chat \
  mr-worker:scrape python3 -m analysis.tier3_extract
# Tier-4 — 3-axis score (wedge .5 / wave .3 / edge .2) → opportunities + opportunity_competitors
docker run --rm --network devcore_net --memory=3g --env-file "$ENVF" \
  -e AN_TIER4_TOP_N=50 mr-worker:scrape python3 -m analysis.tier4_validate
# (Tier-1 embed + Tier-2 cluster = ABANDONED/SKIPPED — CPU-infeasible; do not re-attempt without a GPU)
```

**Crons (root crontab on devcore):** `cron_reddit_daily.sh` (3:30am, new posts) · `cron_reddit_gapscan.sh` (Sun 5:30am) · `restart_backfill.sh` (@reboot). Logs in `/var/log/mr_*`.

**Live containers (canonical):** `mr-reddit-{e,d,fix}` (3 backfill shards), `mr-reddit-daily`, `mr-ollama`, `mr-metabase`, + the `devcore-*` platform. Job containers should be `--rm`.

## The 5-tier funnel (state)
`Tier-0` SQL lexical filter → **`analysis.pain_signals` (10.3M, DONE)** → `Tier-1` embeddings (ABANDONED, CPU-infeasible) → `Tier-2` cluster (SKIPPED) → `Tier-3` `tier3_extract.py` DeepSeek → **`problem_statements`** → `Tier-4` `tier4_validate.py` 3-axis score → **`opportunities`**. As of 2026-06-29: statements=15, opportunities=1 (proof only; scaled run in progress — see `reports/opportunities-v1.md`).

## The scoring framework (enforced by the schema)
`problem_statements` columns = `icp, job_to_be_done, current_workaround, wtp_quotes, severity, frequency_note`. `opportunities` = `wedge_score (.5) + wave_score (.3) + edge_score (.2)`, `saturation_ok` hard-gate (≥3 funded competitors → `final_score` 0), `competitors`, `expansion_vector`. **Selection rule:** wedge-first + a named expansion vector. **Waves:** AI-native ops / vertical AI agents / data sovereignty. **Edge:** ecom/D2C operator + AI-native builder + India cost base. Any agent writing these tables must fill these fields — the Lean-Canvas/WTP/severity framework is structural, not optional. Score against **[[opinions]]** before trusting a rank.

## RULES (every agent obeys — condensed from MASTER-PROMPT §4)
1. **Server-first.** Code lands in the repo + `/opt/market-research`, committed. Code of record runs on `devcore`.
2. **One task = one branch = one PR**, each agent in its own **git worktree** (no collisions).
3. **Test before commit** (unit + e2e where relevant), **then** push. **Never commit secrets** (`worker.env`, `*.env`, `*.key`, `*.pem`). Verify `git status` shows none first.
4. **Cheap models for bulk** (DeepSeek-V3 / GLM-5.2), frontier only for the hardest judgment (Tier-4 judge, hard reviews). Treat the OpenRouter key as money — never fan out expensive frontier calls in loops; stop a job that would blow the budget. Never print the key into logs/commits/PRs.
5. **Cloudflare/anti-bot block → report `CF_BLOCKED` and stop.** Do not loop. (G2/Capterra/GetApp/SoftwareAdvice are hard-blocked.)
6. **Never run heavy analysis while scrapers/crons run** on the 4-core box — serialize heavy jobs (pause the reddit shards for the Tier-3/4 run).
7. **Verify before you destroy.** Any `DROP`/`DELETE`/`TRUNCATE`: `count(*)` to confirm scope, `pg_dump` the table(s) first, apply via a reviewed `migrations/*.sql`. If reality contradicts the docs, STOP and report.
8. **The DB was `ANALYZE`d on 2026-06-29** (first time ever). `count(*)` is still truth; planner estimates are not.
9. **No destructive DB op without a safety net.** Never drop a non-empty table you didn't expect — STOP and report.
10. **Never push to `main`/`production` directly, never force-push, never bypass CI, never `--no-verify`.** PR only; the human merges.
11. **Do not disrupt live collection.** The 3 reddit shards + daily cron are backfilling — don't stop/restart them for unrelated work.
12. **Update this file + `[[opinions]]` at session end** (enforced by the Claude Code `Stop` hook → `scripts/remember.sh`). If a durable opinion emerged, flag it for `opinions.md`.

## Memory & cross-harness sync
- **Source of truth = `AGENTS.md`** (this file). `CLAUDE.md` and `GEMINI.md` are synced copies (Windows can't symlink reliably, so a pre-commit sync keeps them in lockstep). `scripts/sync-memory.sh` re-copies AGENTS.md → both; `scripts/remember.sh` calls it after each session append. One-time setup: `bash scripts/install-hooks.sh` wires the pre-commit hook that self-heals the copies on every commit and refuses a commit that edits a copy without AGENTS.md. **Never edit `CLAUDE.md` or `GEMINI.md` directly — edit `AGENTS.md`, then sync.**
- **Founder beliefs = `[[opinions]]`** (`homelab-vault/opinions.md`, PRIVATE). Read it before any judgment call; weight it over the internet-average answer. Push back explicitly if you have current evidence a belief is stale — name the belief, the evidence, and the change.
- **Reference data = [DATA-DICTIONARY.md](./DATA-DICTIONARY.md)** (every table/source/row count, KEEP/DROP). Trust it over `homelab-vault/Scraper-Registry.md` (stale, superseded).
- **Auto-update:** the Claude Code `Stop` hook (`~/.claude/settings.json`) shells out to `scripts/remember.sh`, which appends the session's concrete learnings under a dated `## Session log` entry and surfaces any durable opinion for `[[opinions]]` review.

### 2026-06-29T15:40:02+05:30 — session smoketest-002
- _session ended (Stop hook fired; agent did not pre-populate REMEMBER_LEARNINGS) — review the transcript if anything material happened._


## Known issues / gaps (don't re-discover these)
- Tier-3 `upsert_statement` has a cosmetic `ON CONFLICT (cluster_id) WHERE cluster_id IS NULL` that is **harmless** (NULLs are distinct → no cap; the 15 statements were a tiny proof run, not a bug). Clean it to a plain `INSERT … RETURNING id`.
- Tier-4 does **no web validation** despite its docstring (`AN_TIER4_WEB_SEARCH` unused) — scores are LLM-only. Implement real web evidence for v2, or rename the claim so we're honest about what the score means.
- **Schema-as-code gaps (TB-A rescues):** `funded_companies`/`vc_firms` exist live with no DDL in repo; 3 drift columns not in committed DDL (`reddit_comments.parent_id`, `pain_signals.deduped`, `problem_clusters.cluster_key`). Capture before any fresh deploy.
- `homelab-vault/Scraper-Registry.md` is stale — DATA-DICTIONARY.md supersedes it.
- `mr-worker:lite` is **not built by compose** — `run_feeds.sh`/`bd_run.sh` fail unless you build it by hand. TB-C wires it in.
- Every `mr-*` job is an ad-hoc `docker run`, not in compose → they leak as `Exited` containers instead of self-cleaning. TB-C fixes this.

## Session log

<!-- `scripts/remember.sh` appends dated entries below. Human-curated; do not let it bloat. -->

