# CONTINUE-HERE — Market-Research Venture: Build & Continuation Guide

> Hand-off doc for any agent (OpenCode + open models, or a fresh Claude session) to pick up this build.
> Written 2026-06-27. Read this top-to-bottom first, then jump to **§7 Next Steps**.

---

## 0. The mission (why any of this exists)

Ashish is building a **data-driven market-research engine** to find a monetizable wedge — a SaaS or product that can reach **$10–20k MRR in 3–4 months** (tight runway; his job ended ~2026-07-09), with a path to scale far larger. We scrape pain points + supply/competition + funding data into a Postgres warehouse, then run a tiered analysis to surface **ranked, web-validated opportunities**.

**Locked selection rule:** WEDGE-FIRST + EXPANSION VECTOR — every candidate must (a) reach $10–20k MRR fast, low-complexity, serving reachable ICPs (agencies, ecom/D2C brands, SaaS operators), AND (b) carry a named expansion path into a structural wave.

**Three structural waves we weight:** AI-native ops tooling · vertical AI agents · data sovereignty / privacy-first.

**Founder edge (scoring axis):** ecom/D2C operator (ran Oneleaf, Majhee) · AI-native builder · India cost base for global delivery.

**Scoring:** `final_score = 0.5·wedge + 0.3·wave + 0.2·edge`; hard gate to 0 if 3+ funded incumbents serve the exact ICP.

Full design: `homelab-vault/Research/Analysis-Architecture.md`. Strategy/scoring background also in `homelab-vault/`.

---

## 1. State dashboard (built / running / pending)

**✅ Built & working**
- Reddit warehouse: `reddit_posts` (~4.9M) ↔ `reddit_comments` (~20M), 80 subreddits, 2020→2026, correlated, idempotent, resumable.
- Reddit crons: daily resume-forward + weekly gap-scan + `@reboot` auto-recovery.
- Supply sources in `documents`/tables: App Store 136k reviews/1.2k apps, Google Play 88k/808, Shopify apps 21.7k, WordPress 10.2k, Atlassian 6k, YC 5.9k, Dev.to/Chrome/etc.
- YouTube pipeline (`connectors/youtube.py`): metadata + json3 captions + comments, idempotent. **Metadata proven on real videos.**
- Analysis free tiers (T0–T2) built + tested (69 unit tests): schema, lexicon FTS filter, Ollama embeddings, clustering.

**🔄 Running (check `docker ps`)**
- Reddit backfill shards (`mr-reddit-fix/ext/d/e`) + the two crons.

**⏳ Pending (the work to continue)**
- YouTube transcript pull — **was blocked on a residential proxy; proxy now obtained.** Launch it.
- Analysis: re-validate T0 recall on a sample → run full free funnel (T0→T1→T2).
- Analysis T3 (DeepSeek extraction) + T4 (web-validation) — **NOT built**; need `OPENROUTER_API_KEY`.
- G2/Capterra connector — parked on Cloudflare/datacenter block; **residential proxy now unblocks it** (richest un-tapped review source).
- Funding/VC connectors → `funded_companies` + `vc_firms` tables.
- YouTube channel classifier (proper LLM pass) → relevant subs → back-catalog scrape.
- Decisions outstanding: "mega" subreddits (r/programming etc.) yes/no.
- Housekeeping: vault MD cleanup, push everything to GitHub, CI/CD setup.

---

## 2. Infrastructure

- **Host:** HP SFF, Proxmox → Debian VM reachable as SSH alias **`devcore`**. 4 cores / 18 GB RAM / **NO GPU**. Power is unstable → **resumability is mandatory** everywhere.
- **Postgres:** container `devcore-postgres`, image `pgvector/pgvector:pg16`, DB `market_research`, role `devcore`. Has `pgvector` (halfvec). Note: app role is NOT the owner of the source tables — **do not `CREATE INDEX` on `reddit_posts`/`reddit_comments`** (use inline FTS / only index the `analysis` schema).
- **Ollama:** container `mr-ollama` (http://mr-ollama:11434), model `qwen3-embedding:0.6b` pulled (embeddings, free).
- **Worker:** images `mr-worker:scrape` (yt-dlp, sklearn, hdbscan, httpx, psycopg2) and `mr-worker:lite`. Code at `/opt/market-research/worker/` (mounted into containers). Secrets in `/opt/market-research/worker.env` (has `PG_DSN`; add `OPENROUTER_API_KEY`, `YT_PROXY` here).
- **Repo (this one):** `homelab-infra`. Market-research stack: `stacks/market-research/` (compose, worker, connectors). **Local repo is source of truth → scp to `/opt/market-research/worker/` to deploy.**
- **Other services:** Metabase (`mr-metabase`, dashboards), Redis, MinIO, Grafana/Prometheus stack, n8n, Traefik. Meilisearch was DROPPED (Postgres FTS/pgvector replaces it).

---

## 3. The pipelines

### Reddit — `connectors/arctic_reddit.py`
- Source: **Arctic Shift API** (free, no creds). Memory-safe streaming (never holds a sub in RAM), per-page flush, comments staged in a session TEMP table.
- **Resume = the DB is the cursor:** each sub starts from `MAX(created_utc)` already stored. `AS_RESUME=0` forces a full re-walk from `AS_AFTER` (fills internal gaps; idempotent).
- Idempotent via `ON CONFLICT (reddit_id)`. Env: `AS_SUBREDDITS, AS_AFTER, AS_BEFORE, AS_RESUME, AS_MAX_PER_SUB, AS_MAX_COMMENTS`.
- Recovery: `/opt/market-research/restart_backfill.sh` (also `@reboot`). Crons: `cron_reddit_daily.sh` (03:30), `cron_reddit_gapscan.sh` (Sun 05:30).

### YouTube — `connectors/youtube.py` + `youtube_analyze.py`
- Metadata via yt-dlp (skip_download, `ignore_no_formats_error`). Transcripts via **yt-dlp json3 captions** (PRIMARY; youtube-transcript-api is a broken-from-server SECONDARY). Comments via getcomments. Idempotent.
- **CRITICAL split (cost + function):** metadata MUST run on the box's home IP (YouTube refuses datacenter IPs → "no player response"). Captions get rate-limited (429) on the box IP, so route **only captions** through a residential proxy. Env: `YT_PROXY` = caption proxy (residential), `YT_META_PROXY` = OFF by default (leave it off). Also `YT_MODE` (urls|channels|keywords), `YT_SEEDS`, `YT_FETCH_COMMENTS`, `YT_COMMENT_LIMIT`, `YT_VIDEO_SLEEP`, `YT_LANGS`.
- Schema: `connectors/youtube_schema.sql` → `youtube_channels/videos/comments/video_insights` (mirrors Reddit's correlated style).
- Seeds: `worker/seeds/` — `core_playlist_video_ids.txt` (726), `watch_later_video_ids.txt` (2726), `relevant_channels_kw.csv` (94, weak keyword pass — redo with an LLM classifier).
- `youtube_analyze.py`: incremental LLM insight extraction (needs `OPENROUTER_API_KEY`).

### Analysis — `connectors/analysis_schema.sql` + `analysis/`
The 5-tier funnel (spend $ inversely to volume). See `Analysis-Architecture.md`.
- `analysis/lexicon.py` — pain SHAPES lexicon (111 phrases: wishes, questions, switching, effort/emotion, lack, cost). Keep-rule = lexicon OR question-shape OR long-detailed; **engagement is a RANKING signal, never a keep-gate** (top-upvoted ≠ pain).
- `analysis/tier0_filter.py` — inline FTS + metadata gates → `analysis.pain_signals`. **Zero DDL on source tables.** Sample mode: `AN_SUBREDDIT`, `AN_LIMIT`, `AN_SOURCE_FILTER`.
- `analysis/tier0_audit.py` — samples rows that did NOT pass, to expand the lexicon empirically.
- `analysis/tier1_embed.py` — Ollama qwen3 → `halfvec(1024)`, commit-per-batch (resumable), priority-order (engagement×dup first), ANN-dedup (<0.08).
- `analysis/tier2_cluster.py` — MiniBatchKMeans → per-bucket HDBSCAN → `problem_clusters` + `trend_acceleration` (SQL slope 2020→2026).
- **T3/T4 not built yet:** T3 = DeepSeek (OpenRouter) over cluster summaries → `problem_statements`. T4 = GPT-Researcher / open_deep_research web-validation (TAM, competitors, saturation, adversarial bear-case) → `opportunities`. Anti-hallucination: cross-source corroboration (≥2 of Reddit/reviews/YouTube), citations NOT NULL, saturation gate.

---

## 4. Conventions (follow these or it breaks)
- **Idempotent everywhere** (`ON CONFLICT DO UPDATE/NOTHING` on natural keys). Re-runs must never duplicate.
- **Memory-safe streaming** — never load a whole dataset into RAM (4 cores/18 GB). Per-page/per-batch flush.
- **Resumable** — power is unstable; commit progress continuously; the DB is the cursor.
- **Env-driven**, `PG_DSN` from env, psycopg2 + `execute_values`. Match `arctic_reddit.py` style.
- **Local repo → scp to `/opt/market-research/worker/` → run via `docker run … mr-worker:scrape python -m <module>`.** Rebuild the image only when deps change.
- **Agent limits:** the account hits session/weekly limits on subagents — do mechanical work (scripts, deploys) in the main loop, not via spawned agents.

---

## 5. Keys & secrets needed (add to `/opt/market-research/worker.env`)
- `OPENROUTER_API_KEY` — DeepSeek/GLM/Kimi for analysis T3/T4 + YouTube insights. (Recharge OpenRouter ~$20–30; pay-per-token; do NOT buy Ollama Cloud — redundant.)
- `YT_PROXY` — Webshare **Rotating Residential** `http://user:pass@host:port` (captions only; ~$3.50/mo, 1 GB is plenty since only captions route through it).

---

## 6. Cost model
- Reddit/embeddings/clustering/metadata = **$0** (free APIs + local box).
- One full analysis run ≈ **$12–20** (DeepSeek bulk + a little Claude/strong-model for the deep-validation judge).
- YouTube transcripts ≈ **$2–3** of residential bandwidth for the whole ~3.5k (captions are tiny).

---

## 7. NEXT STEPS (priority order, with how)
1. **Add `OPENROUTER_API_KEY` + `YT_PROXY` to worker.env** (§5).
2. **Launch YouTube transcript pull** — captions through `YT_PROXY`, metadata on box IP:
   `docker run -d --name mr-youtube-playlists … -e YT_MODE=urls -e YT_FETCH_COMMENTS=0 -e YT_SEEDS="<726 playlist URLs>" …` then Watch Later (2726) with a vlog/music/house-tour title filter applied AFTER metadata resolves.
3. **Re-validate Tier 0** on a sample (`AN_SUBREDDIT=shopify python -m analysis.tier0_filter`) → eyeball `tier0_audit.py` misses → then run the full free funnel T0→T1→T2 (T1 is a multi-day resumable embed).
4. **Build + run T3 (DeepSeek) and T4 (GPT-Researcher web-validation)** → populate `analysis.opportunities`. Review top 15–25.
5. **G2/Capterra connector** (now unblocked by the residential proxy) → biggest un-tapped review source.
6. **Funding/VC connectors** → `funded_companies`, `vc_firms` (also the future fundraising-outreach list).
7. **YouTube channel classifier** (proper LLM pass over the 1,347 subs) → relevant channels → back-catalog scrape.
8. **Housekeeping:** mega-subs decision, vault MD cleanup, push to GitHub, stand up CI/CD.

---

## 8. File map
- `stacks/market-research/worker/connectors/` — `arctic_reddit.py`, `youtube.py`, `youtube_analyze.py`, `youtube_schema.sql`, `analysis_schema.sql`, other source connectors.
- `stacks/market-research/worker/analysis/` — `lexicon.py`, `tier0_filter.py`, `tier1_embed.py`, `tier2_cluster.py`, `tier0_audit.py`.
- `stacks/market-research/worker/seeds/` — YouTube seed lists.
- `stacks/market-research/worker/tests/` — pytest (69 tests).
- On server: `/opt/market-research/worker.env`, `restart_backfill.sh`, `cron_reddit_*.sh`.
- `homelab-vault/Research/Analysis-Architecture.md` — the analysis design (read before building T1–T4).
- `homelab-vault/` — strategy, scoring framework, source registries, ops/cron notes.
