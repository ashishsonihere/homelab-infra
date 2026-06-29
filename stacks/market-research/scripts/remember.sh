#!/usr/bin/env bash
# remember.sh — session-end memory capture for the market-research stack.
#
# Wired to Claude Code's `Stop` hook in ~/.claude/settings.json (and re-usable by
# OpenCode's session-end). It appends the session's concrete learnings to the
# `## Session log` section of AGENTS.md and, if the caller passes an opinion
# draft via $REMEMBER_OPINION, surfaces it for opinions.md review.
#
# Design notes:
# - Idempotent per session: keyed by $REMEMBER_SESSION_ID (or a timestamp) so a
#   re-run doesn't double-append.
# - Human-curated: this script only *appends* dated bullets. It never edits or
#   deletes. The human prunes bloat during review.
# - Never commits secrets: it refuses to run if the payload contains `worker.env`
#   or `OPENROUTER_API_KEY`.
# - Non-fatal: a hook failure should NOT block session end, so we exit 0 on
#   soft errors and only exit non-zero on real environment problems.
#
# Env (set by the hook / caller):
#   REMEMBER_LEARNINGS   multi-line string of concrete session learnings (required to append)
#   REMEMBER_OPINION     optional durable-opinion draft → flagged for opinions.md review
#   REMEMBER_SESSION_ID  optional stable id (default: timestamp); dedupes re-runs
#
# Usage from a hook:
#   REMEMBER_LEARNINGS="$CLAUDE_SESSION_SUMMARY" bash scripts/remember.sh
set -euo pipefail

cd "$(dirname "$0")/.."

AGENTS_MD="AGENTS.md"
LOG_MARKER="## Session log"

if [[ ! -f "$AGENTS_MD" ]]; then
  echo "remember.sh: $AGENTS_MD not found in $(pwd)" >&2
  exit 0   # non-fatal — don't block session end
fi

LEARNINGS="${REMEMBER_LEARNINGS:-}"
HOOK_MODE="${REMEMBER_HOOK:-0}"

# When invoked as a hook with no pre-populated learnings, append a minimal
# "session ended" marker (deduped by SESSION_ID) so the hook firing is visible
# and the human can spot sessions the agent forgot to summarize. The agent is
# expected to call this script itself with REMEMBER_LEARNINGS set (rule 12) —
# the hook is the safety net.
if [[ -z "$LEARNINGS" && -z "${REMEMBER_OPINION:-}" && "$HOOK_MODE" != "1" ]]; then
  # Nothing to remember and not a hook invocation — exit quietly.
  exit 0
fi

# Secret guard: never append a payload that leaks the key.
if grep -qiE 'OPENROUTER_API_KEY|worker\.env|PG_DSN' <<<"$LEARNINGS${REMEMBER_OPINION:-}"; then
  echo "remember.sh: refusing to append a payload that looks like a secret — dropped." >&2
  exit 0
fi

SESSION_ID="${REMEMBER_SESSION_ID:-$(date +%Y%m%dT%H%M%S)}"
STAMP=$(date -Is 2>/dev/null || date "+%Y-%m-%dT%H:%M:%S%z")

# Dedup: if this session_id already has an entry, don't double-append (a re-fired
# hook or a re-run shouldn't bloat the log).
if grep -qF "session $SESSION_ID" "$AGENTS_MD" 2>/dev/null; then
  echo "remember.sh: session $SESSION_ID already logged — skipping (dedup)."
  exit 0
fi

# Build the entry. We insert it right AFTER the `## Session log` marker so newest
# entries sit at the top (human scans newest first).
ENTRY="\n### $STAMP — session $SESSION_ID\n"
if [[ -n "$LEARNINGS" ]]; then
  # Indent each line as a bullet under the heading.
  ENTRY+=$(printf '%s\n' "$LEARNINGS" | sed 's/^/- /')
  ENTRY+="\n"
elif [[ "$HOOK_MODE" == "1" ]]; then
  ENTRY+="- _session ended (Stop hook fired; agent did not pre-populate REMEMBER_LEARNINGS) — review the transcript if anything material happened._\n"
fi
if [[ -n "${REMEMBER_OPINION:-}" ]]; then
  ENTRY+="\n**[[opinions]] candidate — verify with Ashish:**\n"
  ENTRY+=$(printf '%s\n' "${REMEMBER_OPINION:-}" | sed 's/^/  > /')
  ENTRY+="\n"
fi

if ! grep -qF "$LOG_MARKER" "$AGENTS_MD"; then
  printf '\n%s\n<!-- scripts/remember.sh appends dated entries below. -->\n' "$LOG_MARKER" >> "$AGENTS_MD"
fi

# Insert the entry after the persistent comment line if present (so the comment
# stays as a header note and entries stack below it, newest on top); otherwise
# fall back to inserting right after the `## Session log` marker.
if grep -qF "appends dated entries below" "$AGENTS_MD"; then
  ANCHOR='appends dated entries below'
else
  ANCHOR="$LOG_MARKER"
fi
awk -v anchor="$ANCHOR" -v entry="$ENTRY" '
  index($0, anchor) { print; print entry; next }
  { print }
' "$AGENTS_MD" > "$AGENTS_MD.tmp" && mv -f "$AGENTS_MD.tmp" "$AGENTS_MD"

# Keep CLAUDE.md / GEMINI.md in sync so the memory is visible to every harness.
if [[ -x scripts/sync-memory.sh ]]; then
  bash scripts/sync-memory.sh || true
elif [[ -f scripts/sync-memory.sh ]]; then
  sh scripts/sync-memory.sh || true
fi

echo "remember.sh: appended session $SESSION_ID to $AGENTS_MD"
exit 0
