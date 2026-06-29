#!/usr/bin/env bash
# Scrape-image connectors (need selectolax / curl_cffi / google-play-scraper). Scheduled by cron.
# (AlternativeTo excluded — active Cloudflare blocks datacenter proxies; revisit via Bright Data unlocker.)
# Requires the stack compose at /opt/market-research/docker-compose.yml.
set -e
DC="docker compose -f /opt/market-research/docker-compose.yml"
trap '$DC --profile oneshot down >/dev/null 2>&1 || true' EXIT
echo "[$(date -Is)] scrape run start"
for mod in connectors.google_play connectors.appsumo; do
  $DC --profile oneshot run --rm mr-worker-scrape python -m "$mod" || echo "  $mod failed"
done
echo "[$(date -Is)] scrape run done"
