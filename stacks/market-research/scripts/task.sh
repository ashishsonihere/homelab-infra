#!/usr/bin/env bash
# task add "spec text" - enqueues a new task
# task list - shows pending/running tasks
# task status <id> - shows task status
PG_DSN=$(grep ^PG_DSN= /opt/market-research/worker.env | cut -d= -f2-)

case "$1" in
  add)
    psql "$PG_DSN" -c "INSERT INTO agent_tasks (title, spec) VALUES ('$2', '$3')"
    echo "Task queued: $2"
    ;;
  list)
    psql "$PG_DSN" -c "SELECT id, title, status, attempts, created_at FROM agent_tasks ORDER BY created_at DESC LIMIT 20"
    ;;
  status)
    psql "$PG_DSN" -c "SELECT * FROM agent_tasks WHERE id=$2"
    ;;
  *)
    echo "Usage: task add|list|status"
    ;;
esac
