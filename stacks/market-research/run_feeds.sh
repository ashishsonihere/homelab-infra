#!/usr/bin/env bash
# Runs the free API/RSS connectors via the lite image. Scheduled by cron on devcore.
set -e
ENVF=/opt/market-research/worker.env
echo "[$(date -Is)] feeds run start"
for mod in connectors.hackernews connectors.news_rss connectors.ycombinator connectors.stackexchange connectors.producthunt connectors.appstore connectors.wordpress; do
  docker run --rm --network devcore_net --env-file "$ENVF" mr-worker:lite python -m "$mod" || echo "  $mod failed"
done
echo "[$(date -Is)] feeds run done"
