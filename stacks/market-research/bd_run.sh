#!/usr/bin/env bash
# Bright Data runner — used by cron and manually.
#   bd_run.sh trigger backfill   # fire the historical backfill jobs
#   bd_run.sh trigger daily       # fire daily incremental jobs (cron)
#   bd_run.sh collect             # download+ingest ready snapshots (cron, hourly)
# Requires the stack compose at /opt/market-research/docker-compose.yml.
set -e
DC="docker compose -f /opt/market-research/docker-compose.yml"
trap '$DC --profile oneshot down >/dev/null 2>&1 || true' EXIT
$DC --profile oneshot run --rm \
  -e BD_JOBS_FILE=/mr/brightdata_jobs.json -e BD_PENDING_FILE=/mr/bd_pending.jsonl \
  mr-worker-lite python -m connectors.brightdata "$@"
