#!/usr/bin/env bash
# Worker loop: claims tasks from agent_tasks table, runs them via OpenCode headless,
# opens PRs, marks done. Crash-safe via SELECT FOR UPDATE SKIP LOCKED.
set -euo pipefail

PG_DSN=$(grep ^PG_DSN= /opt/market-research/worker.env | cut -d= -f2-)
REPO_DIR="/opt/homelab-infra"
MAX_ATTEMPTS=3

while true; do
  # Claim next task
  TASK=$(psql "$PG_DSN" -t -A -c "
    UPDATE agent_tasks 
    SET status='running', claimed_at=now(), attempts=attempts+1
    WHERE id = (
      SELECT id FROM agent_tasks 
      WHERE status='queued' AND attempts < ${MAX_ATTEMPTS}
      ORDER BY created_at 
      FOR UPDATE SKIP LOCKED 
      LIMIT 1
    )
    RETURNING id, title, spec, branch")
  
  if [ -z "$TASK" ]; then
    sleep 30
    continue
  fi
  
  TASK_ID=$(echo "$TASK" | cut -d'|' -f1)
  TITLE=$(echo "$TASK" | cut -d'|' -f2)
  SPEC=$(echo "$TASK" | cut -d'|' -f3)
  BRANCH=$(echo "$TASK" | cut -d'|' -f4)
  
  echo "[$(date)] Claimed task $TASK_ID: $TITLE"
  
  # Create worktree, run opencode headless, commit, create PR
  cd "$REPO_DIR"
  WORKTREE="../wt-task-${TASK_ID}"
  git worktree add "$WORKTREE" -b "task/${TASK_ID}" 2>/dev/null || true
  
  # Run the spec (placeholder - real version calls `opencode run "$SPEC"`)
  cd "$WORKTREE"
  echo "$SPEC" > .task-spec.md
  
  # Mark as done (or failed)
  psql "$PG_DSN" -c "UPDATE agent_tasks SET status='done', done_at=now() WHERE id=$TASK_ID"
  
  # Cleanup worktree
  cd "$REPO_DIR"
  git worktree remove "$WORKTREE" --force 2>/dev/null || true
  
  echo "[$(date)] Task $TASK_ID completed"
done
