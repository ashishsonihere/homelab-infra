#!/usr/bin/env bash
# Bright Data runner — used by cron and manually.
#   bd_run.sh trigger backfill   # fire the historical backfill jobs
#   bd_run.sh trigger daily       # fire daily incremental jobs (cron)
#   bd_run.sh collect             # download+ingest ready snapshots (cron, hourly)
set -e
docker run --rm --network devcore_net \
  -v /opt/market-research/worker:/app -v /opt/market-research:/mr -w /app \
  -e BD_JOBS_FILE=/mr/brightdata_jobs.json -e BD_PENDING_FILE=/mr/bd_pending.jsonl \
  --env-file /opt/market-research/worker.env \
  mr-worker:lite python -m connectors.brightdata "$@"
