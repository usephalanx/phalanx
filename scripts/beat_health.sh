#!/usr/bin/env bash
# Verify celery-beat is actively dispatching scheduled tasks.
#
# Should be called by `make up` AFTER `docker compose up -d` so beat has
# had time to boot. Catches the silent-failure mode that bit us
# 2026-05-20: forge-beat boots, prints its banner, then crashes inside the
# scheduler init (e.g. `--scheduler=django_celery_beat...` pointing at an
# uninstalled package) — and no scheduled task ever fires. Operators
# could not tell the difference from a healthy stack until reconciler
# rows started piling up PENDING.
#
# Test seams:
#   PHALANX_BEAT_CONTAINER       default: forge-beat
#   PHALANX_BEAT_MAX_WAIT_S      default: 180 (how long to watch for a dispatch)
#   PHALANX_TEST_BEAT_LOG        if set, source of truth instead of docker logs
#                                (allows unit tests without a live stack)
#   PHALANX_SKIP_BEAT_HEALTH=1   emergency bypass (mirrors PHALANX_SKIP_PREFLIGHT)
#
# Exit codes:
#   0  beat is alive + has dispatched at least one task
#   1  beat container missing or not in running state
#   2  beat is "running" but never dispatched a task within the window
#      (the silent-failure case)

set -euo pipefail

if [ "${PHALANX_SKIP_BEAT_HEALTH:-}" = "1" ]; then
  printf "⚠  PHALANX_SKIP_BEAT_HEALTH=1 — beat health check skipped.\n" >&2
  exit 0
fi

CONTAINER="${PHALANX_BEAT_CONTAINER:-forge-beat}"
MAX_WAIT_S="${PHALANX_BEAT_MAX_WAIT_S:-180}"

# ── Test seam: when PHALANX_TEST_BEAT_LOG is set (even to empty string),
#    classify directly from it. Logic mirrors the live path 1:1; the empty
#    case is the "no dispatches ever happened" silent-failure shape.
if [ "${PHALANX_TEST_BEAT_LOG+x}" ]; then
  if echo "${PHALANX_TEST_BEAT_LOG}" | grep -q "Sending due task"; then
    line=$(echo "${PHALANX_TEST_BEAT_LOG}" | grep "Sending due task" | tail -1)
    echo "✅ beat is firing tasks: ${line#*due task }"
    exit 0
  fi
  echo "ERROR: stubbed beat log contained no \"Sending due task\" within window" >&2
  exit 2
fi

# 1. Container present + running
if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  echo "ERROR: ${CONTAINER} not running" >&2
  echo "  hint: docker compose up -d ${CONTAINER}" >&2
  exit 1
fi

status=$(docker inspect --format '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo "missing")
if [ "${status}" != "running" ]; then
  echo "ERROR: ${CONTAINER} status=${status}" >&2
  if [ "${status}" = "restarting" ]; then
    echo "  beat container is crash-looping — last error:" >&2
    docker logs --tail 5 "${CONTAINER}" 2>&1 | sed 's/^/    /' >&2
  fi
  exit 1
fi

# 2. Watch logs for a real "Sending due task" event.
#    The banner alone isn't enough — beat can crash AFTER banner but
#    BEFORE any task is dispatched (the 2026-05-20 shape).
printf "→ waiting up to %ss for %s to dispatch a scheduled task...\n" "${MAX_WAIT_S}" "${CONTAINER}"

elapsed=0
poll_interval=5
while [ "${elapsed}" -lt "${MAX_WAIT_S}" ]; do
  if docker logs --since "${MAX_WAIT_S}s" "${CONTAINER}" 2>&1 | grep -q "Sending due task"; then
    # Re-check status — if beat has transitioned to restarting since the
    # dispatch line was logged, that's still a fail.
    current=$(docker inspect --format '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo "missing")
    if [ "${current}" = "running" ]; then
      latest=$(docker logs --since "${MAX_WAIT_S}s" "${CONTAINER}" 2>&1 | grep "Sending due task" | tail -1)
      printf "✅ beat is firing tasks: %s\n" "${latest#*due task }"
      exit 0
    fi
  fi
  sleep "${poll_interval}"
  elapsed=$((elapsed + poll_interval))
done

echo "ERROR: ${CONTAINER} did not dispatch a scheduled task within ${MAX_WAIT_S}s" >&2
echo "" >&2
echo "  recent log tail:" >&2
docker logs --tail 15 "${CONTAINER}" 2>&1 | sed 's/^/    /' >&2
echo "" >&2
echo "  common causes:" >&2
echo "    1. --scheduler= flag in docker-compose.yml points at an uninstalled" >&2
echo "       package (the 2026-05-20 bug — was django_celery_beat without Django)" >&2
echo "    2. redis connection failing" >&2
echo "    3. celery_app.py beat_schedule is empty" >&2
echo "" >&2
echo "  to bypass: PHALANX_SKIP_BEAT_HEALTH=1 make up" >&2
exit 2
