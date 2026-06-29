# Tooling To Install

> TB-J research doc. **Plan only - do not install.** Covers the senior-engineer toolchain missing on the two target machines. Each entry: what it does, why it matters for *this* market-research data warehouse + analysis pipeline, and copy-paste install commands for both OSes.
>
> **Target machines**
> - **Local Windows 11 box** (has: git, gh, node, npm, python 3.12, uv, WSL; missing: docker, tailscale, and most CLI quality-of-life tools)
> - **Ubuntu server `devcore`** (has: docker, git; missing: most dev tools; runs all containers, Postgres, the worker jobs)
>
> **Project context that drives priority** - Python connectors (`worker/connectors/`) scrape 18 sources into Postgres (`market_research`, schemas `public` + `analysis`); a 5-tier analysis funnel calls OpenRouter (DeepSeek bulk, frontier judge); all jobs run as Docker containers on `devcore`; the laptop has no Docker and drives the server over `ssh devcore`. Near-term blocks that gate priority: **TB-F** (CI = `uv sync` + `ruff` + `pytest`), **TB-K** (`gitleaks` + least-priv role *before* the public push), **TB-C** (compose rewrite), **TB-A** (`migrations/*.sql`).
>
> **Guiding rules** - server-first; cheap models for bulk; never commit `worker.env` / `*.env` / keys; one task = one branch = one PR. Windows-native Docker is intentionally NOT installed (server-first by design).

---

## P0 - Install Now

Tools that block a near-term Task Block, a safety gate, or are depended on by every day-to-day command.

### ruff

- **What:** A single Rust binary that lints *and* formats Python, replacing `black` + `flake8` + `isort` (and `pyupgrade`, `isort`, etc.). 10-100x faster than the stack it replaces.
- **Why for this project:** The worker (`worker/connectors/` - 20+ modules, `worker/analysis/` - 5 tiers, `worker/tests/`) has no linter or formatter today. **TB-F CI** explicitly runs `ruff check --output-format=github` and `ruff format --check` as required checks on `main`. Without ruff locally you cannot get a clean PR. Also catches the kind of drift already in the repo (unused imports in legacy `analyze.py`, mixed quote styles across connectors).
- **Install - Windows (PowerShell):**
  ```powershell
  # uv is already installed - preferred route (keeps ruff in an isolated venv):
  uv tool install ruff
  # OR winget (installs a global binary on PATH):
  winget install --id=astral-sh.ruff -e
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  # Standalone installer (no Python needed on the server):
  curl -LsSf https://astral.sh/ruff/install.sh | sh
  # OR, after bootstrapping uv on the server (see uv entry):
  uv tool install ruff
  # Verify:
  ruff --version
  ```
- **Priority:** **P0** - gates TB-F CI.

### pre-commit

- **What:** A framework that installs and runs configured hook repos (linters, secret scanners) automatically on `git commit` and `git push`, so a bad commit never lands.
- **Why for this project:** This is the glue for the safety rules in §4 of the master prompt. A `.pre-commit-config.yaml` runs `ruff` + `ruff-format` on the worker, `gitleaks` on staged files, and the `end-of-file-fixer`/`trailing-whitespace` hooks. It enforces "never commit secrets" at commit time - the single most important rule for a repo about to go public (TB-F) holding `OPENROUTER_API_KEY`, `PG_DSN`, and Bright Data tokens in a sibling `worker.env`.
- **Install - Windows (PowerShell):**
  ```powershell
  uv tool install pre-commit
  # OR:
  pipx install pre-commit
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  # Ubuntu's apt package is often stale; prefer pipx/uv:
  uv tool install pre-commit
  # After cloning the repo, install the hooks:
  cd ~/homelab-infra && pre-commit install
  ```
- **Priority:** **P0** - required to wire ruff + gitleaks at commit time (TB-K, TB-F).

### gitleaks

- **What:** A fast Go binary that scans git history, staged files, or directories for API keys, tokens, and secrets via regex + entropy rules.
- **Why for this project:** **TB-K mandates a clean `gitleaks detect` run over the whole repo *and its history* before `homelab-infra` goes public** (TB-F). The repo has lived alongside `worker.env` (mode 600, holds `OPENROUTER_API_KEY`, `PG_DSN`, `BRIGHTDATA_API_TOKEN`, source API keys) - the highest-risk artifact in the whole homelab. A single accidental `git add worker.env` at any point in history must be caught *before* the public push, rotated, and scrubbed with `git filter-repo` if found. Also runs as a pre-commit hook so it never happens again.
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=ZacharyRice.Gitleaks -e
  # OR scoop (tends to be newest):
  scoop install gitleaks
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  # Latest release binary:
  LATEST=$(curl -s https://api.github.com/repos/gitleaks/gitleaks/releases/latest | grep -oP '"tag_name": "\K(v[^"]+)')
  curl -L "https://github.com/gitleaks/gitleaks/releases/download/${LATEST}/gitleaks_${LATEST#v}_linux_x64.tar.gz" | tar -xz -C /tmp
  sudo install /tmp/gitleaks /usr/local/bin/
  gitleaks version
  ```
- **Pre-commit hook** (add to `.pre-commit-config.yaml`):
  ```yaml
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2  # pin to a real tag
    hooks:
      - id: gitleaks
  ```
- **Priority:** **P0** - hard security gate before the public push (TB-K, TB-F).

### docker compose v2

- **What:** The `docker compose` subcommand (v2, Go binary shipped as `docker-compose-plugin`) that replaces the old `docker-compose` (Python v1). Same `docker-compose.yml` syntax, faster, no Python dependency.
- **Why for this project:** Every job in this project is a Docker container - the 3 reddit backfill shards, `mr-tier3`, `mr-ollama`, `mr-metabase`, the `mr-worker:scrape`/`:lite` images. **TB-C rewrites `docker-compose.yml` into profiles** (`always-on` / `oneshot` / `analysis`) and converts `run_feeds.sh`, `run_scrape.sh`, `bd_run.sh` from ad-hoc `docker run` to `docker compose --profile ... run --rm ...`. That rewrite requires compose v2 syntax (`compose run --rm`, profiles, `depends_on: condition: service_healthy`). Must verify the server has v2 before authoring the new compose file.
- **Verify (do this now):**
  ```bash
  ssh devcore "docker compose version"
  # Expect: "Docker Compose version v2.x"  (NOT "docker-compose version 1.x")
  ```
- **Install - Windows:** N/A. The laptop intentionally has no Docker (server-first by design). Do NOT install Docker Desktop locally.
- **Install - Ubuntu (devcore) - only if v2 is missing:**
  ```bash
  # Add Docker's official apt repo (gives docker-ce + docker-compose-plugin):
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list
  sudo apt update
  sudo apt install -y docker-compose-plugin
  docker compose version
  ```
- **Priority:** **P0** - required by TB-C; verify before authoring the new compose.

### jq

- **What:** A tiny C binary that slices, filters, and reshapes JSON on the command line (the `sed`/`awk` for JSON).
- **Why for this project:** The repo is full of JSON. `brightdata_jobs.json`, `brightdata_jobs_keyword.json`, `brightdata_jobs_newsubs.json` are job specs you inspect and patch by hand. YouTube connectors emit `json3` caption payloads (see `worker/tests/fixtures/captions_json3.json`, `transcript.json`, `video_info.json`, `comments.json`). The OpenRouter API returns JSON you'll debug (`httpx` responses, DeepSeek tool calls). Arctic Shift responses are JSON. Without `jq` you are squinting at `curl` output; with it, one-liners like `jq '.data[].attributes.title'` replace throwaway Python.
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=jqlang.jq -e
  # OR: scoop install jq ; choco install jq
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  sudo apt update && sudo apt install -y jq
  jq --version
  ```
- **Priority:** **P0** - used in nearly every debugging session across all 18 connectors.

---

## P1 - Soon

Tools that materially improve the daily workflow or unblock a mid-term Task Block, but are not on the critical path of the very next PR.

### uv (project setup)

- **What:** A single Rust binary (from Astral) that is a Python package installer + resolver + virtualenv manager + Python-version manager, replacing `pip`/`virtualenv`/`pip-tools`/`pyenv`/`pipx`. Already installed on the laptop at `~/.local/bin`.
- **Why for this project:** The worker still ships a flat `worker/requirements.txt`. **TB-F CI uses `astral-sh/setup-uv` + `uv sync`** to install deps and run `ruff` + `pytest` - that requires a `pyproject.toml` + `uv.lock` checked into the repo. Migrating `requirements.txt` to `pyproject.toml` is the documented-but-undone prerequisite. Pinning Python 3.12 (the version on the laptop and in the Dockerfiles) via `.python-version` keeps laptop, server, CI, and the `mr-worker:scrape` image in lockstep - the kind of drift that caused the `numpy`/`hdbscan` pin pain already visible in `requirements.txt`.
- **Install - Windows:** Already present. Verify: `uv --version`.
- **Install - Ubuntu (devcore) - bootstrap uv, then it owns the rest:**
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # add ~/.local/bin to PATH per the installer's printed instructions
  uv --version
  ```
- **Project setup (do once, on the laptop, in `worker/`):**
  ```bash
  cd homelab-infra/stacks/market-research/worker
  uv python pin 3.12
  uv init --no-readme --bare        # creates pyproject.toml without restructuring
  # migrate deps:
  uv add httpx yt-dlp youtube-transcript-api pydantic psycopg2-binary python-dotenv \
         numpy scikit-learn hdbscan boto3 redis
  uv add --dev ruff pytest pytest-cov
  uv sync                           # creates .venv + uv.lock
  # run things through uv from now on:
  uv run pytest -q
  uv run ruff check .
  ```
  Add `.venv/` to `.gitignore`; commit `pyproject.toml` + `uv.lock`.
- **Priority:** **P1** - tool is installed, the *setup* is the remaining work; blocks TB-F CI from going green.

### direnv

- **What:** A shell extension that auto-loads/unloads environment variables per directory by running an `.envrc` file when you `cd` in, and unloading when you `cd` out.
- **Why for this project:** `worker.env` (mode 600, holds `OPENROUTER_API_KEY`, `PG_DSN`, source keys) must never be committed, but every connector and analysis tier needs those vars in the environment. Today you either prefix each command with `--env-file` (Docker) or manually `source worker.env` and risk leaking vars into the wrong shell. A `.envrc` containing `source_env_if_exists worker.env` auto-loads them only inside `stacks/market-research/` and nukes them on exit - the cleanest defense against accidentally running `echo $OPENROUTER_API_KEY` from the wrong directory or, worse, pasting it into a commit/PR (§4 rule 3 + 12).
- **Install - Windows:** direnv is Unix-only; run it inside **WSL** (where you already have a shell):
  ```bash
  # In WSL:
  sudo apt update && sudo apt install -y direnv
  echo 'eval "$(direnv hook bash)"' >> ~/.bashrc   # or zsh
  ```
  There is no native PowerShell direnv; on Windows-native shells rely on `$env:` per session, or do Python work in WSL where direnv works.
- **Install - Ubuntu (devcore):**
  ```bash
  sudo apt install -y direnv
  echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
  ```
- **Setup (in the repo):**
  ```bash
  cd homelab-infra/stacks/market-research
  echo 'source_env_if_exists worker.env' > .envrc
  direnv allow
  ```
  **Critical:** add `.envrc` to `.gitignore` (it sources secrets). Never commit `.envrc` or `worker.env`.
- **Priority:** **P1** - security-ergonomic, not on the critical path of the next PR.

### alembic (preferred over sqitch for this project)

- **What:** A Python DB-migration framework (from SQLAlchemy authors) that generates versioned migration scripts and tracks applied versions in an `alembic_version` table.
- **Why for this project:** **TB-A writes `migrations/001_cleanup.sql`** as a plain idempotent SQL file, and there are *already* schema-as-code drift issues documented in AGENTS.md: `funded_companies`/`vc_firms` exist live with zero DDL in the repo; three drift columns (`reddit_comments.parent_id`, `pain_signals.deduped`, `problem_clusters.cluster_key`) are not in committed DDL. A plain `migrations/*.sql` folder works for TB-A's one-shot cleanup, but it has no apply-order, no down-migration, no "which version is the DB at?" answer - exactly the gap that let the drift happen. Alembic (Python, matches the worker's stack and `psycopg2` driver) gives versioned up/down + autogenerate by diffing the live schema against models. **sqitch** (language-agnostic, pure SQL, heavy Perl deps) is the alternative if you want to stay SQL-only and avoid SQLAlchemy coupling; for a Python project that already imports `psycopg2` and `pydantic`, alembic is the lighter fit.
- **Install - Windows (PowerShell):**
  ```powershell
  uv tool install alembic
  # OR: pipx install alembic
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  uv tool install alembic
  ```
- **Setup (on the laptop, in the repo):**
  ```bash
  cd homelab-infra/stacks/market-research
  alembic init migrations
  # point migrations/env.py at $PG_DSN, set script_location = migrations
  # alembic revision --autogenerate -m "capture live schema baseline"
  # alembic upgrade head
  ```
- **Priority:** **P1** - TB-A can ship with plain SQL; alembic is the durable follow-up so drift never recurs.

### httpie

- **What:** A human-friendly HTTP client for the terminal (`http GET url` instead of `curl -s -X GET -H ... url`), with JSON-aware syntax, colorized output, and session support.
- **Why for this project:** Half the connectors are HTTP clients you'll debug against live APIs: Arctic Shift (Reddit history), OpenRouter (DeepSeek chat completions), Bright Data unlocker, Product Hunt, Google Play, TrustRadius. `httpie` lets you replay a connector's exact request in one line and see the JSON response pretty-printed - far faster than adding `print()` to `connectors/*.py` and rerunning the job. Pairs with `jq` for slicing the response.
- **Install - Windows (PowerShell):**
  ```powershell
  uv tool install httpie
  # OR: winget install --id=HTTPie.HTTPie -e
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  uv tool install httpie
  # OR (older): sudo apt install -y httpie
  ```
- **Priority:** **P1** - daily QoL for connector debugging.

### ripgrep (rg)

- **What:** A Rust recursive regex searcher (like `grep -r`) that respects `.gitignore` by default, skips binary files, and is dramatically faster. Also the engine behind many editors' search.
- **Why for this project:** 20+ connector modules, 5 analysis tiers, fixtures, SQL schemas - finding "where is `source_slug` set?" or "which connector writes to `documents`?" is constant. `rg` returns hits in milliseconds across the whole repo and skips `.venv/`/`__pycache__/` automatically. Already the tool the Grep tool here wraps.
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=BurntSushi.ripgrep.MSVC -e
  # OR: scoop install ripgrep ; choco install ripgrep
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  sudo apt install -y ripgrep
  rg --version
  ```
- **Priority:** **P1** - high daily value, tiny install.

### fzf

- **What:** A fuzzy finder that pipes any list (files, git branches, command history, `rg` hits) into an interactive selector.
- **Why for this project:** With 20+ connectors and a multi-branch workflow (one branch per Task Block, each in its own worktree), `git checkout` + file navigation is constant friction. `fzf` powers `Ctrl-R` history search, `**<TAB>` file completion, and `git branch | fzf` switching. Pairs with `rg` via `rg --files | fzf`.
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=junegunn.fzf -e
  # OR: scoop install fzf ; choco install fzf
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  sudo apt install -y fzf
  ```
- **Priority:** **P1** - QoL; installs with ripgrep as a pair.

### tailscale

- **What:** A WireGuard-based mesh VPN. Each machine gets a stable IP on the tailnet; traffic is end-to-end encrypted; ACLs restrict who can reach what; no port forwarding on the router.
- **Why for this project:** Today the laptop reaches the server only on LAN (via SSH). A Mac move is coming and travel happens. Tailscale means the laptop (or phone) can reach the server's Postgres, Metabase, Grafana, and the worker-loop from anywhere - without exposing a single port on the router or punching Cloudflare tunnels for interactive SSH. Also the cleanest way to let CI runners (later) or a second box reach the server without a VPN concentrator. Install on **both** the laptop and server, auth to the same tailnet.
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=Tailscale.Tailscale -e
  # then: tailscale up   (auth via the printed URL)
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up
  ```
- **Follow-up:** replace the LAN IP in `~/.ssh/config` with the server's tailnet IP (or `<hostname>.<tailnet-name>.ts.net` via MagicDNS); add a Tailscale ACL restricting the laptop to just port 22 + the Metabase/Grafana ports.
- **Priority:** **P1** - enables off-LAN work; not on the critical path of the next PR but needed before any travel/Mac move.

---

## P2 - Nice-to-have

Quality-of-life terminal tools. None block a Task Block; all make long sessions at the prompt more pleasant.

### lazygit

- **What:** A terminal UI for git - staged/unstaged diffs, interactive rebase, cherry-pick, log graph, all without typing.
- **Why for this project:** The "one task = one branch = one PR, each in its own worktree" rule means a lot of branch juggling, staging individual hunks across `connectors/` vs `analysis/` vs `tests/`, and rebasing feature branches on `main`. `lazygit` makes hunk-level staging and interactive rebase visual - useful when a single PR touches 20 connectors and you want to split it.
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=JesseDuffield.Lazygit -e
  # OR: scoop install lazygit ; choco install lazygit
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  # No apt package on older Ubuntu; use the GitHub release:
  LATEST=$(curl -s https://api.github.com/repos/jesseduffield/lazygit/releases/latest | grep -oP '"tag_name": "\K(v[^"]+)')
  curl -L "https://github.com/jesseduffield/lazygit/releases/download/${LATEST}/lazygit_${LATEST#v}_Linux_x86_64.tar.gz" | tar -xz -C /tmp lazygit
  sudo install /tmp/lazygit /usr/local/bin/
  ```
- **Priority:** **P2** - pure QoL.

### bat

- **What:** A `cat` clone with syntax highlighting, git integration, and automatic paging.
- **Why for this project:** Reading `db/schema.sql`, `intel_schema.sql`, `saas_schema.sql`, `analysis_schema.sql`, and the connector sources in the terminal is constant. `bat` highlights SQL/Python/YAML and shows `git blame` markers inline. Drop-in alias `alias cat=bat` (or just `bat file`).
- **Install - Windows (PowerShell):**
  ```powershell
  winget install --id=sharkdp.bat -e
  # OR: scoop install bat ; choco install bat
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  sudo apt install -y bat
  # On Debian/Ubuntu the binary is `batcat` (name clash); alias it:
  echo "alias bat='batcat'" >> ~/.bashrc
  ```
- **Priority:** **P2** - pure QoL.

### tmux + neovim

- **What:** `tmux` is a terminal multiplexer (persistent sessions, split panes, detach/reattach over SSH). `neovim` is the modern, Lua-scriptable fork of Vim.
- **Why for this project:** The founder already runs WezTerm + OpenCode/Claude Code + tmux/treehouse in WSL, so the *need* is marginal - this entry is mostly "verify, don't reinstall." Useful on `devcore` over SSH so a long `tier3_extract` run survives a dropped connection (tmux detach), and for quick in-shell edits (`nvim connectors/appsumo.py`) when opening OpenCode is overkill.
- **Install - Windows (WSL only - tmux/neovim are Unix):**
  ```bash
  # In WSL (verify first - may already be present):
  tmux -V || sudo apt install -y tmux
  nvim --version || sudo apt install -y neovim
  ```
- **Install - Ubuntu (devcore):**
  ```bash
  sudo apt install -y tmux neovim
  # For a newer neovim than apt ships, use the PPA or AppImage:
  # curl -LO https://github.com/neovim/neovim/releases/latest/download/nvim.appimage && sudo install nvim.appimage /usr/local/bin/nvim
  ```
- **Priority:** **P2** - already partly present; verify, do not reinstall unless stale.

---

## Quick-reference install matrix

| Tool | Priority | Windows (PowerShell/winget) | Ubuntu (devcore) |
|---|---|---|---|
| ruff | P0 | `uv tool install ruff` | `curl -LsSf https://astral.sh/ruff/install.sh \| sh` |
| pre-commit | P0 | `uv tool install pre-commit` | `uv tool install pre-commit` |
| gitleaks | P0 | `winget install --id=ZacharyRice.Gitleaks -e` | release binary -> `/usr/local/bin` |
| docker compose v2 | P0 | N/A (server-first) | verify `docker compose version`; `apt install docker-compose-plugin` if missing |
| jq | P0 | `winget install --id=jqlang.jq -e` | `apt install jq` |
| uv (setup) | P1 | present | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| direnv | P1 | WSL: `apt install direnv` | `apt install direnv` |
| alembic | P1 | `uv tool install alembic` | `uv tool install alembic` |
| httpie | P1 | `uv tool install httpie` | `uv tool install httpie` |
| ripgrep | P1 | `winget install --id=BurntSushi.ripgrep.MSVC -e` | `apt install ripgrep` |
| fzf | P1 | `winget install --id=junegunn.fzf -e` | `apt install fzf` |
| tailscale | P1 | `winget install --id=Tailscale.Tailscale -e` | `curl -fsSL https://tailscale.com/install.sh \| sh` |
| lazygit | P2 | `winget install --id=JesseDuffield.Lazygit -e` | release binary -> `/usr/local/bin` |
| bat | P2 | `winget install --id=sharkdp.bat -e` | `apt install bat` (alias `bat=batcat`) |
| tmux + neovim | P2 | WSL: `apt install tmux neovim` | `apt install tmux neovim` |

---

## Server bootstrap note

`devcore` currently has only `docker` and `git`. Several P0/P1 tools above are Python (pre-commit, alembic, httpie) or route through `uv`. Bootstrap `uv` on the server once, then install the Python tools via `uv tool install`:

```bash
# One-time bootstrap on devcore:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Follow the printed PATH instruction, then:
uv tool install ruff pre-commit alembic httpie
```

The non-Python tools (`jq`, `ripgrep`, `fzf`, `bat`, `tmux`, `neovim`, `direnv`, `docker-compose-plugin`) come straight from `apt` (see each entry). `gitleaks` and `lazygit` use GitHub release binaries.

## Safety checklist before installing anything

This doc is **plan-only** (TB-J). When execution happens:

1. **Never commit secrets.** Add `.envrc`, `worker.env`, `*.env`, `*.key`, `*.pem` to `.gitignore` *before* installing `direnv` (it creates `.envrc`).
2. **Pre-commit first.** Install `pre-commit` + `gitleaks` + `ruff` and `pre-commit install` *before* any other work that might stage a secret.
3. **Run `gitleaks detect`** over the full history before the TB-F public push (TB-K).
4. **Pin hook versions** in `.pre-commit-config.yaml` (never `latest`); re-audit on bump.
5. **Server-first.** Don't install Docker Desktop on the laptop - the laptop drives the server over `ssh devcore`/tailscale, by design.
