#!/usr/bin/env bash
# Runs the free API/RSS connectors via the lite image (compose oneshot profile).
# Scheduled by cron on devcore. Each module self-removes (--rm); mr-db-wait gates
# on devcore-postgres reachability, then oneshot leftovers are cleaned on exit.
# Requires the stack compose at /opt/market-research/docker-compose.yml
# (deploy once, then: docker compose -f /opt/market-research/docker-compose.yml
#  --profile oneshot build   to build mr-worker:lite / mr-worker:scrape).
set -e
DC="docker compose -f /opt/market-research/docker-compose.yml"
# Clean the readiness gate + any oneshot leftovers on exit (only touches
# oneshot-profile containers; always-on services are untouched — verified).
trap '$DC --profile oneshot down >/dev/null 2>&1 || true' EXIT
echo "[$(date -Is)] feeds run start"
for mod in connectors.hackernews connectors.news_rss connectors.ycombinator connectors.stackexchange connectors.producthunt connectors.appstore connectors.wordpress; do
  $DC --profile oneshot run --rm mr-worker-lite python -m "$mod" || echo "  $mod failed"
done
echo "[$(date -Is)] feeds run done"
