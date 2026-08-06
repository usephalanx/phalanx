#!/usr/bin/env bash
# Run the same three-step migration check the CI workflow runs, locally.
#
# Spins up a throwaway postgres container, runs:
#   1. alembic upgrade head      (fresh DB)
#   2. alembic downgrade base    (proves downgrade is complete)
#   3. alembic upgrade head      (round-trip)
# and asserts the schema fingerprint matches between (1) and (3).
#
# Exits 0 on success, non-zero on the first failure.
#
# Use this before pushing a migration change. The CI workflow will run
# the same checks on every PR that touches alembic/** or phalanx/db/**.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="phalanx-migration-check-$$"
DB_USER="forge"
DB_PASS="check_password"
DB_NAME="forge"
PORT=$(( ( RANDOM % 1000 ) + 25000 ))   # avoid collision with live forge-postgres
DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost:${PORT}/${DB_NAME}"

cleanup() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "→ starting throwaway postgres on port ${PORT}..."
docker run -d --rm --name "${CONTAINER}" \
  -e POSTGRES_USER="${DB_USER}" \
  -e POSTGRES_PASSWORD="${DB_PASS}" \
  -p "${PORT}:5432" \
  pgvector/pgvector:pg16 >/dev/null

# Wait for the throwaway postgres to clear its init dance — same trick as
# scripts/backup_postgres_verify.sh: the image runs a temporary postgres
# on a unix socket, shuts it down, then restarts on TCP.
echo "→ waiting for postgres to clear init dance..."
ready=0
for _ in $(seq 1 60); do
  count=$(docker logs "${CONTAINER}" 2>&1 \
           | grep -c "database system is ready to accept connections" || true)
  if [ "${count}" -ge 2 ] \
     && docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_USER}" -c "SELECT 1;" >/dev/null 2>&1 \
     && sleep 1 \
     && docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_USER}" -c "SELECT 1;" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [ "${ready}" -ne 1 ]; then
  echo "ERROR: postgres never became reachable" >&2
  exit 3
fi

cd "${REPO_ROOT}"

echo "→ Step 1: alembic upgrade head (fresh DB)..."
DATABASE_URL="${DATABASE_URL}" \
  .venv/bin/alembic -c alembic/alembic.ini upgrade head

echo "→ capturing fresh-install fingerprint..."
PGHOST=localhost PGUSER="${DB_USER}" PGPASSWORD="${DB_PASS}" \
PGDATABASE="${DB_NAME}" PHALANX_PG_CONTAINER="${CONTAINER}" \
  ./scripts/migration_schema_fingerprint.sh > /tmp/fingerprint-fresh.txt
echo "    $(wc -l < /tmp/fingerprint-fresh.txt | tr -d ' ') columns observed"

echo "→ Step 2: alembic downgrade base..."
DATABASE_URL="${DATABASE_URL}" \
  .venv/bin/alembic -c alembic/alembic.ini downgrade base

# Assert downgrade left no application tables behind.
REMAINING=$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -A -t -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename != 'alembic_version';")
if [ "${REMAINING}" -ne 0 ]; then
  echo "FAIL: downgrade base left ${REMAINING} application tables behind:" >&2
  docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -c "\dt" >&2
  exit 1
fi

echo "→ Step 3: alembic upgrade head (round-trip)..."
DATABASE_URL="${DATABASE_URL}" \
  .venv/bin/alembic -c alembic/alembic.ini upgrade head

echo "→ capturing round-trip fingerprint..."
PGHOST=localhost PGUSER="${DB_USER}" PGPASSWORD="${DB_PASS}" \
PGDATABASE="${DB_NAME}" PHALANX_PG_CONTAINER="${CONTAINER}" \
  ./scripts/migration_schema_fingerprint.sh > /tmp/fingerprint-roundtrip.txt

if ! diff -u /tmp/fingerprint-fresh.txt /tmp/fingerprint-roundtrip.txt; then
  echo "" >&2
  echo "FAIL: schema fingerprint changed between fresh-install and round-trip." >&2
  echo "      A migration's upgrade and downgrade are not symmetric." >&2
  exit 1
fi

echo ""
echo "✅ migration-bootstrap-check passed"
echo "   columns:          $(wc -l < /tmp/fingerprint-fresh.txt | tr -d ' ')"
echo "   fingerprints:     /tmp/fingerprint-fresh.txt  /tmp/fingerprint-roundtrip.txt"
