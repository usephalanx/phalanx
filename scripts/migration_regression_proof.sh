#!/usr/bin/env bash
# P0-4 regression proof — demonstrates that the migration-bootstrap check
# WOULD have caught the 2026-03-21 bug (UUID/VARCHAR FK type mismatch in
# alembic/versions/20260321_0001_add_dag_columns.py).
#
# This is NOT a CI test — it modifies a committed file. Run it manually
# to prove the safety net works. The script:
#
#   1. Verifies you're on a clean working tree (git status clean).
#   2. Reverts the fix in 20260321_0001_add_dag_columns.py back to the
#      broken `sa.String()` FK columns.
#   3. Runs `make migration-check`.
#   4. Asserts the check FAILS with the exact "type mismatch" error.
#   5. Restores the file from git (no permanent damage).
#
# Exits 0 if the check correctly caught the bug; non-zero if it didn't
# (which would mean the safety net is broken).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${REPO_ROOT}/alembic/versions/20260321_0001_add_dag_columns.py"

cd "${REPO_ROOT}"

# ── Guard: clean tree ────────────────────────────────────────────────────────
if ! git diff --quiet -- "${TARGET}"; then
  echo "ERROR: ${TARGET} has uncommitted changes." >&2
  echo "Stash or commit first; this script reverts the file to a broken state" >&2
  echo "and restores it from git afterwards." >&2
  exit 1
fi

cleanup() {
  echo "→ restoring ${TARGET} from git..."
  git checkout -- "${TARGET}"
  echo "   restored OK"
}
trap cleanup EXIT

# ── Step 1: revert the fix ───────────────────────────────────────────────────
echo "→ reverting UUID→String fix to recreate the 2026-03-21 bug..."
python3 -c "
import re
from pathlib import Path
p = Path('${TARGET}')
src = p.read_text()
# Strip the UUID import we added in the fix.
src = re.sub(
    r'\nfrom sqlalchemy\\.dialects\\.postgresql import UUID\n',
    '\n', src,
)
# Re-introduce the original VARCHAR FK shape.
src = src.replace(
    'sa.Column(\"id\", UUID(as_uuid=False), primary_key=True),',
    'sa.Column(\"id\", sa.String(), primary_key=True),',
)
src = src.replace(
    'sa.Column(\"task_id\", UUID(as_uuid=False), sa.ForeignKey(\"tasks.id\"), nullable=False),',
    'sa.Column(\"task_id\", sa.String(), sa.ForeignKey(\"tasks.id\"), nullable=False),',
)
src = src.replace(
    'sa.Column(\"depends_on_id\", UUID(as_uuid=False), sa.ForeignKey(\"tasks.id\"), nullable=False),',
    'sa.Column(\"depends_on_id\", sa.String(), sa.ForeignKey(\"tasks.id\"), nullable=False),',
)
p.write_text(src)
"
echo "   reverted; diff shows broken state:"
git diff --stat -- "${TARGET}"

# ── Step 2: run the check; expect failure ────────────────────────────────────
echo ""
echo "→ running make migration-check (expecting FAILURE)..."
if ./scripts/migration_bootstrap_check.sh 2>&1 | tee /tmp/regression-output.log; then
  echo ""
  echo "❌ REGRESSION SAFETY NET BROKEN" >&2
  echo "   The migration check passed even with the known-bad migration." >&2
  echo "   This means P0-4 would NOT have caught the 2026-03-21 bug." >&2
  exit 1
fi

# ── Step 3: confirm the failure reason matches the original bug ──────────────
if grep -qE "foreign key constraint .* cannot be implemented|incompatible types: character varying and uuid" \
   /tmp/regression-output.log; then
  echo ""
  echo "✅ regression-proof PASSED"
  echo "   The check correctly refused the known-bad migration with:"
  grep -E "foreign key constraint|incompatible types" /tmp/regression-output.log | head -2
else
  echo ""
  echo "⚠  check failed, but not with the expected error." >&2
  echo "   Inspect /tmp/regression-output.log to verify the failure is real." >&2
  exit 1
fi
