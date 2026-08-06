#!/usr/bin/env bash
# Restore a pg_dump file into a target postgres container.
#
# WARNING: destructive on the target database — drops + recreates it.
#
# Usage:
#   scripts/backup_postgres_restore.sh <dump-file>
#
# Env overrides:
#   PHALANX_PG_TARGET     default: forge-postgres-restore-test (must already be running)
#   PHALANX_PG_USER       default: forge
#   PHALANX_PG_NAME       default: forge

set -euo pipefail

DUMP="${1:-}"
TARGET="${PHALANX_PG_TARGET:-forge-postgres-restore-test}"
DB_USER="${PHALANX_PG_USER:-forge}"
DB_NAME="${PHALANX_PG_NAME:-forge}"

if [ -z "${DUMP}" ] || [ ! -f "${DUMP}" ]; then
  echo "Usage: $0 <dump-file>" >&2
  echo "" >&2
  echo "Restores into container '${TARGET}'. Override target with PHALANX_PG_TARGET=." >&2
  echo "Safety: this DROPs ${DB_NAME} on the target before restoring." >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "${TARGET}"; then
  echo "ERROR: target container '${TARGET}' is not running" >&2
  exit 2
fi

if [ "${TARGET}" = "forge-postgres" ] && [ "${PHALANX_RESTORE_ALLOW_PROD:-}" != "1" ]; then
  echo "REFUSED: target is the live forge-postgres container." >&2
  echo "Set PHALANX_RESTORE_ALLOW_PROD=1 to confirm overwriting it." >&2
  exit 1
fi

echo "Restoring $(basename "${DUMP}") into ${TARGET}/${DB_NAME}..."

docker exec "${TARGET}" psql -U "${DB_USER}" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS ${DB_NAME};" \
  -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" >/dev/null

docker exec -i "${TARGET}" pg_restore -U "${DB_USER}" -d "${DB_NAME}" \
  --no-owner --no-acl --exit-on-error < "${DUMP}"

echo "OK restored into ${TARGET}/${DB_NAME}"
