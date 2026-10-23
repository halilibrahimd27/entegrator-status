#!/usr/bin/env bash
# Restore drill: load a dump into a scratch database next to the live one, then check
# that the schema is current, every secret decrypts and the site renders from it.
#
#   tools/restore-drill.sh backups/sentinel-<UTC time>.dump
#
# The live database is never touched. The scratch database is dropped at the end.
set -euo pipefail

cd "$(dirname "$0")/.."
dump="${1:?usage: tools/restore-drill.sh <dump file>}"
test -s "$dump"
scratch="sentinel_restore_drill"
compose=(docker compose -f compose.prod.yml)

set -a
# shellcheck disable=SC1091
. ./.env
set +a

cleanup() {
  "${compose[@]}" exec -T postgres dropdb --username sentinel --if-exists "$scratch" || true
}
trap cleanup EXIT

"${compose[@]}" exec -T postgres dropdb --username sentinel --if-exists "$scratch"
"${compose[@]}" exec -T postgres createdb --username sentinel "$scratch"
"${compose[@]}" exec -T postgres \
  pg_restore --username sentinel --dbname "$scratch" --no-owner --exit-on-error < "$dump"

"${compose[@]}" run --rm --no-deps \
  -e DATABASE_URL="postgresql+asyncpg://sentinel:${POSTGRES_PASSWORD}@postgres:5432/${scratch}" \
  web python -m sentinel.db.drill
