#!/usr/bin/env bash
# Scrape-image connectors (need selectolax / curl_cffi / google-play-scraper). Scheduled by cron.
# (AlternativeTo excluded — active Cloudflare blocks datacenter proxies; revisit via Bright Data unlocker.)
set -e
ENVF=/opt/market-research/worker.env
echo "[$(date -Is)] scrape run start"
for mod in connectors.google_play connectors.appsumo; do
  docker run --rm --network devcore_net -v /opt/market-research/worker:/app -v /opt/market-research:/mr -w /app \
    --env-file "$ENVF" mr-worker:scrape python -m "$mod" || echo "  $mod failed"
done
echo "[$(date -Is)] scrape run done"
