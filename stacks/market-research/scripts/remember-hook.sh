#!/usr/bin/env bash
# remember-hook.sh — Claude Code Stop-hook adapter.
#
# Claude Code fires the `Stop` hook at session end and pipes a JSON payload on
# stdin: { "session_id": "...", "transcript_path": "...", "cwd": "..." }.
# This wrapper parses the payload (no jq dependency — grep/sed only, so it works
# on the bare Windows Git Bash), exports the bits remember.sh needs, and calls
# it in hook mode. The agent is ALSO expected (AGENTS.md rule 12) to call
# `scripts/remember.sh` directly with `REMEMBER_LEARNINGS` set before stopping —
# that's the primary path; this hook is the safety net that guarantees *something*
# is appended even when the agent forgets.
#
# Wired in ~/.claude/settings.json:
#   "hooks": { "Stop": [{ "matcher": "", "hooks": [{ "type": "command",
#       "command": "bash <repo>/stacks/market-research/scripts/remember-hook.sh" }] }] }
#
# Non-fatal: never exits non-zero (a hook failure must not block session end).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read the JSON payload from stdin (Claude Code pipes it in). If stdin is empty
# (e.g. manual test), fall back to env or a timestamp.
PAYLOAD=""
if [[ ! -t 0 ]]; then
  PAYLOAD="$(cat || true)"
fi

# Extract session_id without jq: match "session_id": "value"
SESSION_ID="$(printf '%s' "$PAYLOAD" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' || true)"
if [[ -z "$SESSION_ID" ]]; then
  SESSION_ID="$(date +%Y%m%dT%H%M%S)"
fi

# Extract cwd (so we run remember.sh against the right stack). Default to the
# repo stack dir if absent.
CWD="$(printf '%s' "$PAYLOAD" | grep -oE '"cwd"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | sed -E 's/.*"cwd"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' || true)"

# If cwd looks like it's inside the market-research stack, run there; otherwise
# default to the canonical stack path. This keeps the hook safe when Claude is
# launched from a different repo.
STACK_DIR="$SCRIPT_DIR/.."
if [[ -n "$CWD" && -f "$CWD/AGENTS.md" ]]; then
  STACK_DIR="$CWD"
fi

export REMEMBER_HOOK=1
export REMEMBER_SESSION_ID="$SESSION_ID"

# Run remember.sh from the stack dir so it finds AGENTS.md.
cd "$STACK_DIR"
bash "$SCRIPT_DIR/remember.sh" || true

exit 0
