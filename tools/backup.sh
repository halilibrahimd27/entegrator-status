#!/usr/bin/env bash
# Nightly PostgreSQL backup of the production stack (compose.prod.yml).
#
#   tools/backup.sh                 # writes backups/sentinel-<UTC time>.dump
#   KEEP=30 tools/backup.sh         # keep the newest 30 dumps (default 14)
#
# The dump streams out of the container to the host, so no volume permissions are
# involved; files are created mode 600. Copy them off this machine (docs/BACKUP.md).
# A dump holds personal data and sealed credentials: treat it like the database.
set -euo pipefail

cd "$(dirname "$0")/.."
KEEP="${KEEP:-14}"
mkdir -p backups
umask 077

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="backups/sentinel-${stamp}.dump"
docker compose -f compose.prod.yml exec -T postgres \
  pg_dump --username sentinel --dbname sentinel --format custom --compress 6 \
  > "${target}.partial"
mv "${target}.partial" "${target}"

# Keep the newest $KEEP dumps.
ls -1t backups/sentinel-*.dump | tail -n "+$((KEEP + 1))" | while read -r old; do
  rm -- "$old"
done
echo "$target"
