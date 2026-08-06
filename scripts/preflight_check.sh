#!/usr/bin/env bash
# Phalanx preflight — runs before `docker compose up` to prevent the
# 2026-05-11 class of incident: docker silently created a fresh empty
# postgres volume because the previous one was orphaned.
#
# Exit codes:
#   0  all checks passed → make up continues
#   1  hard refusal      → operator must fix the underlying issue
#   2  soft refusal      → overridable with a documented env flag
#
# Override flags (read from environment):
#   PHALANX_ALLOW_FRESH_BOOT=1   allow a missing/fresh postgres volume
#   PHALANX_ALLOW_EXTERNAL_DB=1  allow DATABASE_URL pointing outside compose
#   PHALANX_SKIP_PREFLIGHT=1     skip everything (emergency only)
#
# Test seams (unset in normal operation):
#   PHALANX_TEST_VOLUMES             comma-separated volume names to claim exist
#   PHALANX_TEST_VOLUME_CREATED_AT   override the CreatedAt for stubbed volumes
#   PHALANX_TEST_DOCKER_INFO_OK      "1" → docker daemon "running"; "0" → not
#   PHALANX_TEST_REPO_ROOT           override repo root (defaults to script dir)

set -euo pipefail

# ── Colors (skip if stdout isn't a TTY) ──────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  CL_RED=$'\033[0;31m'; CL_GREEN=$'\033[0;32m'; CL_YELLOW=$'\033[0;33m'
  CL_BOLD=$'\033[1m'; CL_DIM=$'\033[2m'; CL_RESET=$'\033[0m'
else
  CL_RED=""; CL_GREEN=""; CL_YELLOW=""; CL_BOLD=""; CL_DIM=""; CL_RESET=""
fi

# ── Config ───────────────────────────────────────────────────────────────────
REPO_ROOT="${PHALANX_TEST_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
EXPECTED_COMPOSE_NAME="phalanx-dev"
EXPECTED_POSTGRES_VOLUME="${EXPECTED_COMPOSE_NAME}_forge-postgres-data"
EXPECTED_DB_HOST_PATTERN="@postgres:"   # compose-internal DNS
EXPECTED_DB_NAME="forge"

ENV_FILE="${REPO_ROOT}/.env"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.yml"
BACKUPS_DIR="${REPO_ROOT}/backups/postgres"
LEDGER_JSONL_PATH="${PHALANX_LEDGER_JSONL_PATH:-${REPO_ROOT}/ledger.jsonl}"

# ── Skip everything if requested ─────────────────────────────────────────────
if [ "${PHALANX_SKIP_PREFLIGHT:-}" = "1" ]; then
  printf "%s⚠  PHALANX_SKIP_PREFLIGHT=1 — preflight skipped.%s\n" "$CL_YELLOW" "$CL_RESET" >&2
  exit 0
fi

# ── Tracking state ───────────────────────────────────────────────────────────
HARD_FAIL=0
SOFT_FAIL=0
WARNINGS=0
REPORT=""        # accumulates the per-check report lines
FAIL_HINTS=""    # accumulates "to proceed" guidance

emit_pass() { REPORT+="  ${CL_GREEN}✓${CL_RESET} $1"$'\n'; }
emit_warn() { REPORT+="  ${CL_YELLOW}!${CL_RESET} $1"$'\n'; WARNINGS=$((WARNINGS+1)); }
emit_fail() { REPORT+="  ${CL_RED}✗${CL_RESET} $1"$'\n'; HARD_FAIL=$((HARD_FAIL+1)); }
emit_soft() { REPORT+="  ${CL_RED}✗${CL_RESET} $1"$'\n'; SOFT_FAIL=$((SOFT_FAIL+1)); }
emit_detail() { REPORT+="      ${CL_DIM}$1${CL_RESET}"$'\n'; }

# ── Test seams ───────────────────────────────────────────────────────────────
volume_exists() {
  # Returns 0 if volume exists, 1 otherwise.
  local name="$1"
  if [ -n "${PHALANX_TEST_VOLUMES:-}" ]; then
    echo "${PHALANX_TEST_VOLUMES}" | tr ',' '\n' | grep -qx "${name}"
    return $?
  fi
  docker volume inspect "${name}" >/dev/null 2>&1
}

volume_created_at() {
  local name="$1"
  if [ -n "${PHALANX_TEST_VOLUME_CREATED_AT:-}" ]; then
    echo "${PHALANX_TEST_VOLUME_CREATED_AT}"
    return 0
  fi
  docker volume inspect --format '{{.CreatedAt}}' "${name}" 2>/dev/null || echo "unknown"
}

volume_mountpoint() {
  local name="$1"
  if [ -n "${PHALANX_TEST_VOLUMES:-}" ]; then
    echo "/var/lib/docker/volumes/${name}/_data"
    return 0
  fi
  docker volume inspect --format '{{.Mountpoint}}' "${name}" 2>/dev/null || echo "unknown"
}

docker_daemon_ok() {
  if [ -n "${PHALANX_TEST_DOCKER_INFO_OK:-}" ]; then
    [ "${PHALANX_TEST_DOCKER_INFO_OK}" = "1" ]
    return $?
  fi
  docker info >/dev/null 2>&1
}

# ── Helpers ──────────────────────────────────────────────────────────────────
humanize_age() {
  # Convert an ISO 8601 (or docker's "2026-05-11 21:20:19 +0000 UTC") timestamp
  # to a human-readable age. Returns empty on parse failure.
  local ts="$1"
  local epoch
  epoch=$(date -u -j -f "%Y-%m-%d %H:%M:%S" "${ts%% +*}" "+%s" 2>/dev/null || true)
  if [ -z "${epoch}" ]; then
    # Try ISO-with-T format
    epoch=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "${ts}" "+%s" 2>/dev/null || true)
  fi
  [ -z "${epoch}" ] && return 0
  local now diff
  now=$(date -u +%s)
  diff=$((now - epoch))
  if [ "${diff}" -lt 60 ]; then echo "${diff}s ago"
  elif [ "${diff}" -lt 3600 ]; then echo "$((diff/60)) min ago"
  elif [ "${diff}" -lt 86400 ]; then echo "$((diff/3600)) h ago"
  else echo "$((diff/86400)) d ago"; fi
}

# ── Print header ─────────────────────────────────────────────────────────────
printf "\n%s┌──────────────────────────────────────────────────────────┐%s\n" "$CL_BOLD" "$CL_RESET"
printf "%s│  Phalanx preflight check                                 │%s\n"     "$CL_BOLD" "$CL_RESET"
printf "%s└──────────────────────────────────────────────────────────┘%s\n\n"   "$CL_BOLD" "$CL_RESET"

# ── Check 1: Docker daemon ───────────────────────────────────────────────────
if docker_daemon_ok; then
  emit_pass "docker daemon is running"
else
  emit_fail "docker daemon is not running"
  emit_detail "start Docker Desktop and re-run \`make up\`"
  FAIL_HINTS+="  • start Docker Desktop"$'\n'
fi

# ── Check 2: docker-compose.yml has expected project name ────────────────────
if [ ! -f "${COMPOSE_FILE}" ]; then
  emit_fail "docker-compose.yml not found at ${COMPOSE_FILE}"
  FAIL_HINTS+="  • this script must run from the repo root"$'\n'
else
  ACTUAL_NAME=$(grep -E "^name:" "${COMPOSE_FILE}" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"' | tr -d "'")
  if [ "${ACTUAL_NAME}" = "${EXPECTED_COMPOSE_NAME}" ]; then
    emit_pass "compose project name is ${EXPECTED_COMPOSE_NAME}"
  else
    emit_fail "compose project name mismatch"
    emit_detail "expected: ${EXPECTED_COMPOSE_NAME}"
    emit_detail "found   : ${ACTUAL_NAME:-<missing>}"
    emit_detail "a wrong project name silently mounts a fresh volume — DATA LOSS RISK"
    FAIL_HINTS+="  • restore the line \`name: ${EXPECTED_COMPOSE_NAME}\` in docker-compose.yml"$'\n'
  fi
fi

# ── Check 3: .env exists ─────────────────────────────────────────────────────
if [ -f "${ENV_FILE}" ]; then
  emit_pass ".env exists"
else
  emit_fail ".env is missing"
  emit_detail "compose expects \`env_file: .env\` — services won't start without it"
  FAIL_HINTS+="  • cp .env.example .env  (then fill in API keys)"$'\n'
fi

# ── Check 4: DATABASE_URL points to compose-internal postgres ────────────────
if [ -f "${ENV_FILE}" ]; then
  DB_URL_LINE=$(grep -E "^DATABASE_URL=" "${ENV_FILE}" 2>/dev/null || true)
  if [ -z "${DB_URL_LINE}" ]; then
    # Not in .env — settings.py's default applies. That default IS the right host.
    emit_pass "DATABASE_URL uses settings.py default (\`@postgres:5432/forge\`)"
  else
    DB_URL="${DB_URL_LINE#DATABASE_URL=}"
    if echo "${DB_URL}" | grep -qE "${EXPECTED_DB_HOST_PATTERN}" \
       && echo "${DB_URL}" | grep -qE "/${EXPECTED_DB_NAME}\$|/${EXPECTED_DB_NAME}\?"; then
      emit_pass "DATABASE_URL points to compose-internal \`postgres:5432/${EXPECTED_DB_NAME}\`"
    elif [ "${PHALANX_ALLOW_EXTERNAL_DB:-}" = "1" ]; then
      emit_warn "DATABASE_URL points outside compose — allowed by PHALANX_ALLOW_EXTERNAL_DB=1"
      emit_detail "host pattern check: expected ${EXPECTED_DB_HOST_PATTERN}, value redacted"
    else
      emit_soft "DATABASE_URL does not point to compose-internal postgres"
      emit_detail "expected host pattern: ${EXPECTED_DB_HOST_PATTERN}"
      emit_detail "value (redacted): $(echo "${DB_URL}" | sed -E 's|//[^:]+:[^@]+@|//USER:PASS@|')"
      emit_detail "services will connect elsewhere — verify this is intentional"
      FAIL_HINTS+="  • if intentional: PHALANX_ALLOW_EXTERNAL_DB=1 make up"$'\n'
      FAIL_HINTS+="  • otherwise restore DATABASE_URL=postgresql+asyncpg://...@postgres:5432/forge"$'\n'
    fi
  fi
fi

# ── Check 5: Postgres volume exists (THE 2026-05-11 GUARD) ───────────────────
if volume_exists "${EXPECTED_POSTGRES_VOLUME}"; then
  CREATED=$(volume_created_at "${EXPECTED_POSTGRES_VOLUME}")
  AGE=$(humanize_age "${CREATED}")
  MOUNT=$(volume_mountpoint "${EXPECTED_POSTGRES_VOLUME}")
  emit_pass "postgres volume found: ${EXPECTED_POSTGRES_VOLUME}"
  emit_detail "created : ${CREATED}${AGE:+  (${AGE})}"
  emit_detail "mount   : ${MOUNT}"
  # Warn if the volume is suspiciously young (< 5 min). Catches "just bootstrapped
  # but didn't realize the data was wiped" case.
  if [ -n "${AGE}" ] && echo "${AGE}" | grep -qE "^[0-9]+s ago$|^[0-4] min ago$"; then
    emit_warn "volume is very new (${AGE}) — confirm this is intentional"
    emit_detail "if you just ran \`docker compose down -v\` or re-installed, this is expected"
    emit_detail "if not, the previous volume may have been replaced silently"
  fi
elif [ "${PHALANX_ALLOW_FRESH_BOOT:-}" = "1" ]; then
  emit_warn "postgres volume MISSING — allowed by PHALANX_ALLOW_FRESH_BOOT=1"
  emit_detail "compose will create a fresh empty volume on \`up\`"
  emit_detail "after first dispatch, run: make backup  &&  make ledger-verify"
else
  emit_soft "postgres volume not found: ${EXPECTED_POSTGRES_VOLUME}"
  emit_detail "${CL_RED}DATA LOSS RISK${CL_RESET}: \`docker compose up\` will create a fresh empty volume."
  emit_detail "the previous volume (if any) is now orphaned."
  FAIL_HINTS+="  • If this is a clean first-time setup: PHALANX_ALLOW_FRESH_BOOT=1 make up"$'\n'
  FAIL_HINTS+="  • If data should still exist, DO NOT continue — investigate first:"$'\n'
  FAIL_HINTS+="      docker volume ls | grep postgres"$'\n'
  FAIL_HINTS+="      make backup-list                  # available restores"$'\n'
  FAIL_HINTS+="      cat docs/ops/backups.md           # restore drill"$'\n'
fi

# ── Check 6: backups/ writable ───────────────────────────────────────────────
if mkdir -p "${BACKUPS_DIR}" 2>/dev/null && [ -w "${BACKUPS_DIR}" ]; then
  emit_pass "backups/postgres/ is writable"
else
  emit_fail "backups/postgres/ is not writable"
  emit_detail "path: ${BACKUPS_DIR}"
  FAIL_HINTS+="  • check filesystem perms on ${REPO_ROOT}"$'\n'
fi

# ── Check 7: ledger.jsonl path is writable ───────────────────────────────────
LEDGER_DIR="$(dirname "${LEDGER_JSONL_PATH}")"
if mkdir -p "${LEDGER_DIR}" 2>/dev/null && touch -c "${LEDGER_JSONL_PATH}" 2>/dev/null && [ -w "${LEDGER_DIR}" ]; then
  emit_pass "ledger.jsonl path is writable: ${LEDGER_JSONL_PATH}"
else
  emit_fail "ledger.jsonl path is not writable: ${LEDGER_JSONL_PATH}"
  emit_detail "P0-2 export would silently fail without this"
  FAIL_HINTS+="  • check perms on ${LEDGER_DIR}"$'\n'
fi

# ── Print report ─────────────────────────────────────────────────────────────
printf "%s" "${REPORT}"
echo ""

# ── Decide ───────────────────────────────────────────────────────────────────
if [ "${HARD_FAIL}" -gt 0 ]; then
  printf "%s❌ Preflight REFUSED — %d hard failure(s)%s\n\n" "$CL_RED" "$HARD_FAIL" "$CL_RESET"
  printf "To proceed:\n%s\n" "${FAIL_HINTS}"
  printf "%sdocker compose up will NOT run.%s\n\n" "$CL_RED" "$CL_RESET"
  exit 1
fi

if [ "${SOFT_FAIL}" -gt 0 ]; then
  printf "%s❌ Preflight REFUSED — %d safety check(s) tripped%s\n\n" "$CL_RED" "$SOFT_FAIL" "$CL_RESET"
  printf "To proceed:\n%s\n" "${FAIL_HINTS}"
  printf "%sdocker compose up will NOT run.%s\n\n" "$CL_RED" "$CL_RESET"
  exit 2
fi

if [ "${WARNINGS}" -gt 0 ]; then
  printf "%s⚠  Preflight passed with %d warning(s).%s\n" "$CL_YELLOW" "$WARNINGS" "$CL_RESET"
  if [ "${PHALANX_ALLOW_FRESH_BOOT:-}" = "1" ]; then
    printf "\n%sFresh boot mode:%s after first dispatch lands, run:\n" "$CL_BOLD" "$CL_RESET"
    printf "    make backup\n"
    printf "    make ledger-verify\n"
  fi
  echo ""
  exit 0
fi

printf "%s✅ All preflight checks passed.%s\n\n" "$CL_GREEN" "$CL_RESET"
exit 0
