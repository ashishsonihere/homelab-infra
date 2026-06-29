#!/usr/bin/env bash
# Runs ON the server (devcore). Creates a least-privilege DB role `mr_worker` for the scraper,
# with a password generated server-side (never printed), and writes the worker's env file.
set -e
PW=$(openssl rand -hex 24)
PSQL="docker exec devcore-postgres psql -U devcore -d market_research -v ON_ERROR_STOP=0"

$PSQL -c "CREATE ROLE mr_worker LOGIN PASSWORD '$PW';" 2>/dev/null || true
$PSQL -c "ALTER ROLE mr_worker LOGIN PASSWORD '$PW';"
$PSQL -c "GRANT USAGE ON SCHEMA public TO mr_worker;
          GRANT ALL ON ALL TABLES IN SCHEMA public TO mr_worker;
          GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO mr_worker;
          ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO mr_worker;
          ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO mr_worker;"

printf 'PG_DSN=postgresql://mr_worker:%s@devcore-postgres:5432/market_research\nREDIS_URL=redis://devcore-redis:6379/4\n' "$PW" > /opt/market-research/worker.env
chmod 600 /opt/market-research/worker.env
echo "OK: mr_worker role set; worker.env written ($(wc -c < /opt/market-research/worker.env) bytes)"
