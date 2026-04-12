#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# healthcheck.sh — Verify WordPress is running and configured.
#
# Usage:
#   ./scripts/healthcheck.sh [URL]
# ──────────────────────────────────────────────────────────────────

set -euo pipefail

URL="${1:-http://localhost:8080}"
ERRORS=0

check() {
    local label="$1"
    local endpoint="$2"
    local expect="$3"

    response=$(curl -s -o /dev/null -w "%{http_code}" "${endpoint}" 2>/dev/null || echo "000")

    if [ "${response}" = "${expect}" ]; then
        echo "  [PASS] ${label} — HTTP ${response}"
    else
        echo "  [FAIL] ${label} — HTTP ${response} (expected ${expect})"
        ERRORS=$((ERRORS + 1))
    fi
}

echo "WordPress Health Check: ${URL}"
echo "─────────────────────────────────"

check "Homepage loads"          "${URL}/"            "200"
check "Admin login page"        "${URL}/wp-login.php" "200"
check "REST API available"      "${URL}/wp-json/"     "200"
check "404 page works"          "${URL}/nonexistent-page-xyz" "404"

echo "─────────────────────────────────"
if [ "${ERRORS}" -eq 0 ]; then
    echo "All checks passed."
    exit 0
else
    echo "${ERRORS} check(s) failed."
    exit 1
fi
