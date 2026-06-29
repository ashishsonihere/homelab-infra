#!/usr/bin/env bash
# install-hooks.sh — one-time setup for the market-research memory hooks.
#
# Copies the version-controlled hook sources in scripts/githooks/ into the repo's
# .git/hooks/ (the standard, non-clobbering location — does NOT use
# core.hooksPath, so it won't drop any other hooks you may have) and makes them
# executable.
#
# Safe to re-run (idempotent). Run once after cloning:
#   bash stacks/market-research/scripts/install-hooks.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SRC="$SCRIPT_DIR/githooks"
ROOT="$(git rev-parse --show-toplevel)"
HOOK_DST="$ROOT/.git/hooks"

if [[ ! -d "$HOOK_DST" ]]; then
  echo "install-hooks.sh: $HOOK_DST not found — is this a git repo?" >&2
  exit 1
fi

for hook in "$HOOK_SRC"/*; do
  [[ -f "$hook" ]] || continue
  name="$(basename "$hook")"
  cp -f "$hook" "$HOOK_DST/$name"
  chmod +x "$HOOK_DST/$name" 2>/dev/null || true   # Windows may ignore the bit; git runs it anyway
  echo "installed $name -> .git/hooks/$name"
done

echo "done. pre-commit now keeps CLAUDE.md + GEMINI.md synced to AGENTS.md."
