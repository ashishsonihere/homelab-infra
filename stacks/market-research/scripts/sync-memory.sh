#!/usr/bin/env bash
# sync-memory.sh — keep CLAUDE.md + GEMINI.md in lockstep with AGENTS.md.
# Windows can't symlink reliably (and git on Windows treats symlinks as text), so
# we ship plain copies and re-sync them before every commit. Run via pre-commit
# hook or manually: `bash scripts/sync-memory.sh`.
#
# AGENTS.md is the source of truth. Never edit CLAUDE.md or GEMINI.md directly —
# edit AGENTS.md, then run this.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f AGENTS.md ]]; then
  echo "sync-memory.sh: AGENTS.md not found in $(pwd)" >&2
  exit 1
fi

for copy in CLAUDE.md GEMINI.md; do
  cp -f AGENTS.md "$copy"
  echo "synced AGENTS.md -> $copy"
done
