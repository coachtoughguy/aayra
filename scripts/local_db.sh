#!/usr/bin/env bash
# Rebuild a local dev database from the exact migrations that are live in Supabase.
# Requires a running Postgres (16+) reachable via PGHOST/PGPORT/PGUSER (defaults: /tmp, 54322, postgres).
set -euo pipefail
cd "$(dirname "$0")/.."
export PGHOST="${PGHOST:-/tmp}" PGPORT="${PGPORT:-54322}" PGUSER="${PGUSER:-postgres}"
DB="${1:-aayra_dev}"
psql -q -d postgres -c "DROP DATABASE IF EXISTS ${DB} WITH (FORCE)" -c "CREATE DATABASE ${DB}"
psql -q -v ON_ERROR_STOP=1 -d "$DB" -f supabase/local/00_supabase_compat.sql
for f in supabase/migrations/*.sql; do
  psql -q -v ON_ERROR_STOP=1 -1 -d "$DB" -f "$f" >/dev/null
done
echo "Built ${DB}: $(psql -tAd "$DB" -c "select count(*) from pg_tables where schemaname='public'") tables"
