#!/usr/bin/env bash
# Canonical launcher for the cifix worker sidecar.
#
# Why this script exists:
# The cifix worker mounts /var/run/docker.sock so the SRE setup task
# can spawn sandbox containers via `docker run`. The socket's GID
# differs by host:
#   - Linux: typically 999 or 998 (the host's "docker" group)
#   - macOS Docker Desktop: 0 (root group inside the Docker VM)
# The script detects the in-container GID and threads it through with
# `--group-add` so the worker's non-root `forge` user gets socket access
# without escalating to UID 0.
#
# Required env (from your shell):
#   OPENAI_API_KEY
#   ANTHROPIC_API_KEY
#   GH_TOKEN
#
# Usage:
#   bash scripts/start_cifix_worker.sh
#
# Emergency override (NOT recommended — full root in the worker):
#   PHALANX_CIFIX_AS_ROOT=1 bash scripts/start_cifix_worker.sh

set -euo pipefail

# Auto-source .env so OPENAI/ANTHROPIC keys land inside the container even
# when the host shell hasn't exported them. (Polish-pass finding #7.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -f "${REPO_ROOT}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${REPO_ROOT}/.env"
  set +a
fi

CONTAINER_NAME="forge-cifix-worker"
NETWORK="phalanx-dev_forge-net"
IMAGE="phalanx-worker:latest"
CONCURRENCY="${PHALANX_CIFIX_CONCURRENCY:-8}"
# Option β (2026-05-20): pre-baked sandbox base image with apt deps pre-installed.
# Empty = original python:3.12-slim behavior (per env_detector).
SANDBOX_BASE_OVERRIDE="${PHALANX_SANDBOX_PYTHON_BASE_IMAGE_OVERRIDE:-phalanx-sandbox-base:py312-prebaked-v1}"

# ── Detect docker.sock GID as the container will see it ──────────────────────
# Probe via a throwaway alpine container with the socket mounted. Portable
# across Linux + macOS hosts because we ask the container kernel, not the
# host kernel, what GID the socket has.
echo "→ detecting /var/run/docker.sock GID inside containers..."
SOCKET_GID="$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock alpine \
              stat -c '%g' /var/run/docker.sock 2>/dev/null)"
if ! [[ "${SOCKET_GID}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: could not detect socket GID (got: ${SOCKET_GID!r})" >&2
  exit 2
fi
echo "   socket GID = ${SOCKET_GID}"

# ── Build security args ──────────────────────────────────────────────────────
SECURITY_ARGS=()
if [ "${PHALANX_CIFIX_AS_ROOT:-}" = "1" ]; then
  echo "⚠  PHALANX_CIFIX_AS_ROOT=1 — running cifix worker as root."
  echo "   Use this only for emergency triage; --group-add is the supported path."
  SECURITY_ARGS+=( "--user" "root" )
else
  SECURITY_ARGS+=( "--group-add" "${SOCKET_GID}" )
  echo "   --group-add ${SOCKET_GID} (forge user gets socket-group access; UID stays 1001)"
fi

# ── Tear down any existing instance ──────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "→ removing existing ${CONTAINER_NAME}..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

# ── Launch ───────────────────────────────────────────────────────────────────
echo "→ starting ${CONTAINER_NAME} (concurrency=${CONCURRENCY})..."
docker run -d --rm --name "${CONTAINER_NAME}" \
  --network "${NETWORK}" \
  "${SECURITY_ARGS[@]}" \
  -e PHALANX_WORKER=1 \
  -e DATABASE_URL="postgresql+asyncpg://forge:forge_dev_password@postgres:5432/forge" \
  -e CELERY_BROKER_URL="redis://redis:6379/0" \
  -e CELERY_RESULT_BACKEND="redis://redis:6379/1" \
  -e REDIS_URL="redis://redis:6379/0" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  -e GH_TOKEN="${GH_TOKEN:-}" \
  -e PHALANX_SANDBOX_PYTHON_BASE_IMAGE_OVERRIDE="${SANDBOX_BASE_OVERRIDE}" \
  -v /Users/raj/forge:/app \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /app \
  "${IMAGE}" \
  celery -A phalanx.queue.celery_app worker \
    --loglevel=info \
    --queues=cifix_commander,cifix_techlead,cifix_engineer,cifix_sre,cifix_sre_verify,cifix_challenger \
    --concurrency="${CONCURRENCY}" \
    --hostname=worker-cifix@%h >/dev/null

echo "→ waiting for worker ready..."
for _ in $(seq 1 30); do
  if docker logs "${CONTAINER_NAME}" 2>&1 | grep -q "worker-cifix.*ready"; then
    echo "   ready."
    break
  fi
  sleep 1
done

# ── Verify socket access from inside the container ───────────────────────────
echo "→ verifying \`docker ps\` works from inside ${CONTAINER_NAME}..."
if docker exec "${CONTAINER_NAME}" docker ps -q >/dev/null 2>&1; then
  echo "   ✅ socket access confirmed"
else
  echo "   ❌ docker ps failed inside container — socket permission still broken" >&2
  echo "   diagnostic:"
  docker exec "${CONTAINER_NAME}" docker ps 2>&1 | head -3 >&2 || true
  exit 3
fi

echo ""
echo "✅ cifix worker started with --group-add ${SOCKET_GID}"
