#!/usr/bin/env bash
# Sync local pg dumps to an off-host destination via rclone.
#
# Required env:
#   PHALANX_BACKUP_REMOTE  rclone remote spec, e.g. "r2:phalanx-backups/postgres/"
#                          or "local:/tmp/phalanx-backup-test/" for local testing.
#
# Optional env:
#   PHALANX_BACKUP_DIR        default: <repo>/backups/postgres
#   PHALANX_REMOTE_MIN_AGE    delete remote dumps older than this (rclone --min-age)
#                              default: 30d (set to 0 to disable remote pruning)
#
# Behavior:
#   1. Copies any local dump that doesn't already exist at the remote
#      (rclone copy dedupes by size + mtime, so re-running is idempotent).
#   2. Prunes remote dumps older than PHALANX_REMOTE_MIN_AGE.
#
# Exit codes:
#   0 success (or no-op)
#   1 usage / config error
#   2 rclone not installed
#   3 nothing to upload
#   4 rclone copy failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${PHALANX_BACKUP_DIR:-${REPO_ROOT}/backups/postgres}"
REMOTE="${PHALANX_BACKUP_REMOTE:-}"
REMOTE_MIN_AGE="${PHALANX_REMOTE_MIN_AGE:-30d}"

if [ -z "${REMOTE}" ]; then
  echo "ERROR: PHALANX_BACKUP_REMOTE not set" >&2
  echo "  Example: export PHALANX_BACKUP_REMOTE='r2:phalanx-backups/postgres/'" >&2
  echo "  Or for local testing: export PHALANX_BACKUP_REMOTE='local:/tmp/phalanx-backup-test/'" >&2
  exit 1
fi

if ! command -v rclone >/dev/null 2>&1; then
  echo "ERROR: rclone not installed. Install with: brew install rclone" >&2
  echo "  Then configure a remote: rclone config" >&2
  exit 2
fi

if ! ls -1 "${BACKUP_DIR}"/forge-*.dump >/dev/null 2>&1; then
  echo "ERROR: no dumps in ${BACKUP_DIR} — run scripts/backup_postgres.sh first" >&2
  exit 3
fi

echo "Syncing ${BACKUP_DIR} -> ${REMOTE} ..."
if ! rclone copy --include 'forge-*.dump' --no-traverse "${BACKUP_DIR}" "${REMOTE}"; then
  echo "ERROR: rclone copy failed" >&2
  exit 4
fi

if [ "${REMOTE_MIN_AGE}" != "0" ]; then
  echo "Pruning remote dumps older than ${REMOTE_MIN_AGE} ..."
  rclone delete --include 'forge-*.dump' --min-age "${REMOTE_MIN_AGE}" "${REMOTE}" || true
fi

echo "OK $(ls -1 "${BACKUP_DIR}"/forge-*.dump | wc -l | tr -d ' ') local dump(s) synced to ${REMOTE}"
