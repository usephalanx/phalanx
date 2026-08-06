#!/usr/bin/env bash
# Verify the latest pg_dump by restoring it into a throwaway postgres container
# and comparing row counts against the live container for critical tables.
#
# Env overrides:
#   PHALANX_BACKUP_DIR    default: <repo>/backups/postgres
#   PHALANX_PG_CONTAINER  default: forge-postgres  (the live source)
#   PHALANX_PG_USER       default: forge
#   PHALANX_PG_NAME       default: forge
#   PHALANX_VERIFY_DUMP   default: <latest in backup dir>
#
# Exit codes:
#   0 verified (or live tables not present — empty stack verified at structural level)
#   1 usage / config error
#   2 no dumps to verify
#   3 throwaway container failed to come up
#   4 pg_restore failed
#   5 row-count mismatch

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${PHALANX_BACKUP_DIR:-${REPO_ROOT}/backups/postgres}"
SOURCE_CONTAINER="${PHALANX_PG_CONTAINER:-forge-postgres}"
DB_USER="${PHALANX_PG_USER:-forge}"
DB_NAME="${PHALANX_PG_NAME:-forge}"

DUMP="${PHALANX_VERIFY_DUMP:-$(ls -1t "${BACKUP_DIR}"/forge-*.dump 2>/dev/null | head -1)}"
if [ -z "${DUMP}" ] || [ ! -f "${DUMP}" ]; then
  echo "ERROR: no dump found in ${BACKUP_DIR}" >&2
  exit 2
fi

VERIFY_CONTAINER="phalanx-backup-verify-$$"
cleanup() { docker rm -f "${VERIFY_CONTAINER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "Verifying $(basename "${DUMP}") ..."

# Spin up a throwaway postgres with the same major version as forge-postgres.
# Intentionally do NOT pass POSTGRES_DB — the image's init script would race
# with our CREATE DATABASE below.
docker run -d --rm --name "${VERIFY_CONTAINER}" \
  -e POSTGRES_USER="${DB_USER}" \
  -e POSTGRES_PASSWORD=verify_only_password \
  pgvector/pgvector:pg16 >/dev/null

# The postgres image's init runs a temporary postgres on a unix socket FIRST,
# then SHUTS IT DOWN before restarting on TCP. So a single successful probe is
# not enough — we need to wait past the shutdown phase. Strategy: wait for the
# "database system is ready to accept connections" log line to appear at least
# twice (once for the temp socket-only phase, once for the final TCP phase).
ready=0
for _ in $(seq 1 60); do
  count=$(docker logs "${VERIFY_CONTAINER}" 2>&1 \
           | grep -c "database system is ready to accept connections" || true)
  if [ "${count}" -ge 2 ]; then
    # Final phase reached — confirm reachable for 2 consecutive probes.
    if docker exec "${VERIFY_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT 1;" >/dev/null 2>&1 \
        && sleep 1 \
        && docker exec "${VERIFY_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT 1;" >/dev/null 2>&1; then
      ready=1
      break
    fi
  fi
  sleep 1
done
if [ "${ready}" -ne 1 ]; then
  echo "ERROR: throwaway postgres did not become reachable in 60s" >&2
  docker logs "${VERIFY_CONTAINER}" 2>&1 | tail -20 >&2
  exit 3
fi

# The postgres image auto-creates a DB matching POSTGRES_USER (=DB_NAME here)
# when POSTGRES_DB is unset. Restore directly into it.

if ! docker exec -i "${VERIFY_CONTAINER}" pg_restore -U "${DB_USER}" -d "${DB_NAME}" \
       --no-owner --no-acl --exit-on-error < "${DUMP}" 2>/tmp/pg_restore_err.$$; then
  echo "ERROR: pg_restore failed:" >&2
  cat /tmp/pg_restore_err.$$ >&2
  rm -f /tmp/pg_restore_err.$$
  exit 4
fi
rm -f /tmp/pg_restore_err.$$

# Compare row counts for critical tables. If the live source isn't reachable,
# treat the restore as structurally verified (still useful — the dump parsed
# and replayed without error).
TABLES=("shadow_ledger" "ci_integrations" "runs" "tasks")
src_reachable=1
docker exec "${SOURCE_CONTAINER}" pg_isready -U "${DB_USER}" >/dev/null 2>&1 || src_reachable=0

mismatch=0
for t in "${TABLES[@]}"; do
  dst=$(docker exec "${VERIFY_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" \
        -t -A -c "SELECT count(*) FROM ${t};" 2>/dev/null || echo "missing")
  if [ "${src_reachable}" -eq 1 ]; then
    src=$(docker exec "${SOURCE_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" \
          -t -A -c "SELECT count(*) FROM ${t};" 2>/dev/null || echo "missing")
    if [ "${src}" = "${dst}" ]; then
      printf "  OK   %-20s  src=%s  dst=%s\n" "${t}" "${src}" "${dst}"
    else
      printf "  FAIL %-20s  src=%s  dst=%s\n" "${t}" "${src}" "${dst}"
      mismatch=$((mismatch + 1))
    fi
  else
    printf "  OK   %-20s  dst=%s  (source unreachable, structural-only)\n" "${t}" "${dst}"
  fi
done

if [ "${mismatch}" -gt 0 ]; then
  echo "FAIL: ${mismatch} table(s) mismatch in $(basename "${DUMP}")" >&2
  exit 5
fi

echo "OK $(basename "${DUMP}") verified"
