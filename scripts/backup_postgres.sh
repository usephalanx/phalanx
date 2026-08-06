#!/usr/bin/env bash
# Phalanx postgres backup — pg_dump of forge-postgres, timestamped, with retention prune.
#
# Env overrides:
#   PHALANX_BACKUP_DIR        default: <repo>/backups/postgres
#   PHALANX_BACKUP_RETENTION  default: 14 dumps (~3.5 days at 6h cadence)
#   PHALANX_PG_CONTAINER      default: forge-postgres
#   PHALANX_PG_USER           default: forge
#   PHALANX_PG_NAME           default: forge
#
# Exit codes:
#   0 success
#   1 usage / config error
#   2 container not running
#   3 pg_dump failed
#   4 dump self-check failed (pg_restore --list)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${PHALANX_BACKUP_DIR:-${REPO_ROOT}/backups/postgres}"
RETENTION="${PHALANX_BACKUP_RETENTION:-14}"
CONTAINER="${PHALANX_PG_CONTAINER:-forge-postgres}"
DB_USER="${PHALANX_PG_USER:-forge}"
DB_NAME="${PHALANX_PG_NAME:-forge}"

mkdir -p "${BACKUP_DIR}"

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  echo "ERROR: container '${CONTAINER}' is not running" >&2
  exit 2
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/forge-${TS}.dump"
TMP="${OUT}.tmp"

# pg_dump in custom format (compressed, pg_restore-friendly)
if ! docker exec "${CONTAINER}" pg_dump -U "${DB_USER}" -F c -d "${DB_NAME}" > "${TMP}"; then
  rm -f "${TMP}"
  echo "ERROR: pg_dump failed" >&2
  exit 3
fi

# Self-check: the file must parse as a valid pg_restore archive
if ! docker exec -i "${CONTAINER}" pg_restore --list < "${TMP}" >/dev/null 2>&1; then
  rm -f "${TMP}"
  echo "ERROR: dump self-check failed (pg_restore --list)" >&2
  exit 4
fi

mv "${TMP}" "${OUT}"

SIZE=$(wc -c < "${OUT}" | tr -d ' ')
echo "OK ${OUT} (${SIZE} bytes)"

# Retention prune — keep newest RETENTION dumps
cd "${BACKUP_DIR}"
KEEP=$((RETENTION))
# shellcheck disable=SC2012
ls -1t forge-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f -- "${old}"
  echo "pruned ${old}"
done
