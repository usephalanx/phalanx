#!/usr/bin/env bash
# Print a stable, sortable fingerprint of the current postgres schema:
# one line per (table, column, type, nullable, default). Excludes alembic's
# own bookkeeping table.
#
# Used by:
#   - .github/workflows/migration-bootstrap.yml to compare fresh-install
#     vs round-trip schemas
#   - scripts/migration_bootstrap_check.sh for the same check locally
#
# Env (defaults match the dev compose):
#   PGHOST, PGUSER, PGPASSWORD, PGDATABASE
#   PHALANX_PG_CONTAINER  default: forge-postgres
#                         set to "" or "host" to use psql directly
#                         (e.g. against a GH Actions service postgres)

set -euo pipefail

DB_USER="${PGUSER:-forge}"
DB_NAME="${PGDATABASE:-forge}"
CONTAINER="${PHALANX_PG_CONTAINER:-forge-postgres}"

QUERY=$(cat <<'SQL'
SELECT
    c.table_name,
    c.column_name,
    c.data_type,
    COALESCE(c.character_maximum_length::text, '-') AS char_max,
    c.is_nullable,
    COALESCE(c.column_default, '-')                 AS column_default
FROM information_schema.columns c
WHERE c.table_schema = 'public'
  AND c.table_name != 'alembic_version'
ORDER BY c.table_name, c.column_name;
SQL
)

if [ -n "${CONTAINER}" ] && [ "${CONTAINER}" != "host" ] && docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  docker exec -e PGPASSWORD="${PGPASSWORD:-}" "${CONTAINER}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -A -t -F '|' -c "${QUERY}"
else
  PGPASSWORD="${PGPASSWORD:-forge_dev_password}" \
    psql -h "${PGHOST:-localhost}" -U "${DB_USER}" -d "${DB_NAME}" -A -t -F '|' -c "${QUERY}"
fi
