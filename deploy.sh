#!/bin/bash
# FORGE — Deploy to AWS Lightsail
#
# Build images LOCALLY (Mac, linux/amd64), transfer to server, restart.
# The server never runs `docker build` — it only loads pre-built images.
#
# Usage:
#   ./deploy.sh           # auto-bump patch version
#   ./deploy.sh v0.2.0    # explicit version
#   ./deploy.sh --migrate-only   # run DB migrations without redeploying images

set -euo pipefail

SERVER="ubuntu@44.233.157.41"
APP_DIR="/home/ubuntu/forge"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM="linux/amd64"

# ── SSH key resolution (same fallback chain as sandbox) ──────────────────
if [ -n "${LIGHTSAIL_KEY:-}" ] && [ -f "$LIGHTSAIL_KEY" ]; then
  SSH_KEY="$LIGHTSAIL_KEY"
elif [ -f "$HOME/work/LightsailDefaultKey-us-west-2.pem" ]; then
  SSH_KEY="$HOME/work/LightsailDefaultKey-us-west-2.pem"
elif [ -f "$HOME/work/aws/LightsailDefaultKey-us-west-2.pem" ]; then
  SSH_KEY="$HOME/work/aws/LightsailDefaultKey-us-west-2.pem"
elif [ -f "$HOME/.ssh/LightsailDefaultKey-us-west-2.pem" ]; then
  SSH_KEY="$HOME/.ssh/LightsailDefaultKey-us-west-2.pem"
else
  echo "ERROR: Lightsail SSH key not found. Set LIGHTSAIL_KEY env var or place the key at:"
  echo "  $HOME/work/LightsailDefaultKey-us-west-2.pem"
  echo "  $HOME/work/aws/LightsailDefaultKey-us-west-2.pem"
  echo "  $HOME/.ssh/LightsailDefaultKey-us-west-2.pem"
  exit 1
fi

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=10"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

# ── Migrate-only mode ────────────────────────────────────────────────────
if [ "${1:-}" = "--migrate-only" ]; then
  echo "▶ Running DB migrations only..."
  $SSH "$SERVER" "cd $APP_DIR && docker compose run --rm phalanx-migrate"
  echo "✓ Migrations complete."
  exit 0
fi

# ── Release tag ──────────────────────────────────────────────────────────
if [ -n "${1:-}" ]; then
  RELEASE_TAG="$1"
else
  LAST_TAG=$(git tag --sort=-v:refname | head -n 1 2>/dev/null || echo "")
  if [ -z "$LAST_TAG" ]; then
    RELEASE_TAG="v0.1.0"
  else
    RELEASE_TAG=$(echo "$LAST_TAG" | awk -F. '{printf "%s.%s.%d", $1, $2, $3+1}')
  fi
fi

echo "╔══════════════════════════════════════════════╗"
echo "║  FORGE Deploy                                ║"
echo "║  Release : $RELEASE_TAG"
echo "║  Target  : $SERVER"
echo "║  App dir : $APP_DIR"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Step 1: Build images locally (linux/amd64) ───────────────────────────
echo "▶ [1/6] Building phalanx-api image (linux/amd64)..."
docker build --platform "$PLATFORM" \
  --target production \
  -t phalanx-api:latest \
  -t "phalanx-api:$RELEASE_TAG" \
  "$REPO_DIR"

echo ""
echo "▶ [1/6] Building phalanx-worker image..."
# Worker uses the same Dockerfile — production target
docker build --platform "$PLATFORM" \
  --target production \
  -t phalanx-worker:latest \
  -t "phalanx-worker:$RELEASE_TAG" \
  "$REPO_DIR"

# ── Step 2: Save images as compressed tarballs ───────────────────────────
echo ""
echo "▶ [2/6] Saving images to tarballs..."
docker save phalanx-api:latest | gzip > /tmp/phalanx-api.tar.gz
docker save phalanx-worker:latest | gzip > /tmp/phalanx-worker.tar.gz

API_SIZE=$(du -h /tmp/phalanx-api.tar.gz | cut -f1)
WORKER_SIZE=$(du -h /tmp/phalanx-worker.tar.gz | cut -f1)
echo "  phalanx-api:    $API_SIZE"
echo "  phalanx-worker: $WORKER_SIZE"

# ── Step 3: Upload images + configs to server ────────────────────────────
echo ""
echo "▶ [3/6] Uploading images to server..."
$SCP /tmp/phalanx-api.tar.gz /tmp/phalanx-worker.tar.gz "$SERVER:/tmp/"

echo "▶ [3/6] Uploading configs..."
$SCP "$REPO_DIR/docker-compose.prod.yml" "$SERVER:$APP_DIR/docker-compose.yml"

# Sync skill-registry and configs (no images, just YAML)
[ -d "$REPO_DIR/skill-registry" ] && rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.gitkeep' \
  "$REPO_DIR/skill-registry/" "$SERVER:$APP_DIR/skill-registry/" || true

[ -d "$REPO_DIR/configs" ] && rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  --exclude '.gitkeep' \
  "$REPO_DIR/configs/" "$SERVER:$APP_DIR/configs/" || true

# Sync landing page + nginx config
[ -d "$REPO_DIR/site" ] && rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  "$REPO_DIR/site/" "$SERVER:$APP_DIR/site/" || true

[ -f "$REPO_DIR/nginx/nginx.conf" ] && rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  "$REPO_DIR/nginx/" "$SERVER:$APP_DIR/nginx/" || true

# Upload .env.prod (must exist locally — not committed to git)
if [ -f "$REPO_DIR/.env.prod" ]; then
  $SCP "$REPO_DIR/.env.prod" "$SERVER:$APP_DIR/.env"
  echo "  ✓ .env.prod uploaded as .env"
else
  echo "  WARNING: .env.prod not found locally — server will use existing .env"
fi

# ── Step 4: Load images and restart on server ────────────────────────────
echo ""
echo "▶ [4/6] Loading images and restarting services on server..."
$SSH "$SERVER" bash -s <<'REMOTE'
set -e
cd /home/ubuntu/forge

echo "  Loading phalanx-api image..."
docker load < /tmp/phalanx-api.tar.gz

echo "  Loading phalanx-worker image..."
docker load < /tmp/phalanx-worker.tar.gz

rm -f /tmp/phalanx-api.tar.gz /tmp/phalanx-worker.tar.gz

echo "  Running DB migrations..."
docker compose run --rm phalanx-migrate < /dev/null

echo "  Stopping old containers..."
docker compose down --remove-orphans 2>/dev/null || true

echo "  Starting all services (no build)..."
docker compose up -d --no-build

echo "  Waiting for API health check..."
for i in $(seq 1 18); do
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' phalanx-prod-phalanx-api-1 2>/dev/null || echo "starting")
  if [ "$STATUS" = "healthy" ]; then
    echo "  ✓ API healthy after ~$((i * 10))s"
    break
  fi
  if [ "$i" = "18" ]; then
    echo "  WARNING: API not healthy after 180s — check logs"
    docker logs phalanx-prod-phalanx-api-1 --tail 30
  fi
  sleep 10
done

echo ""
echo "  Container status:"
docker compose ps
echo ""
echo "  Memory:"
free -h | head -2
echo ""
echo "  Container memory:"
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'

# Cleanup old images
docker image prune -f 2>/dev/null || true
REMOTE

# ── Step 5: Verify deployment ────────────────────────────────────────────
echo ""
echo "▶ [5/6] Verifying deployment..."
sleep 5
HTTP_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" \
  "http://44.233.157.41:8000/health" --max-time 10 || echo "000")

if [ "$HTTP_HEALTH" = "200" ]; then
  echo "  ✓ API health: $HTTP_HEALTH"
else
  echo "  ✗ API health: $HTTP_HEALTH — deployment may need investigation"
  echo "    Run: ssh -i \$SSH_KEY ubuntu@44.233.157.41 'docker logs phalanx-prod-phalanx-api-1 --tail 50'"
fi

# ── Step 6: Tag release ──────────────────────────────────────────────────
echo ""
echo "▶ [6/6] Tagging release $RELEASE_TAG..."
LAST_TAG_FOR_LOG=$(git tag --sort=-v:refname | head -n 1 2>/dev/null || echo "")
if [ -n "$LAST_TAG_FOR_LOG" ]; then
  RELEASE_NOTES=$(git log "$LAST_TAG_FOR_LOG"..HEAD \
    --pretty=format:"- %s" --no-merges 2>/dev/null || echo "- Deploy update")
else
  RELEASE_NOTES=$(git log --pretty=format:"- %s" --no-merges -10 2>/dev/null || echo "- Initial release")
fi

git tag -a "$RELEASE_TAG" -m "$(cat <<EOF
Release $RELEASE_TAG — $(date +%Y-%m-%d)

$RELEASE_NOTES
EOF
)" 2>/dev/null || echo "  Tag $RELEASE_TAG already exists, skipping"

# ── Cleanup local tarballs ───────────────────────────────────────────────
rm -f /tmp/phalanx-api.tar.gz /tmp/phalanx-worker.tar.gz

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Deploy complete: $RELEASE_TAG               ║"
echo "║  http://44.233.157.41:8000/health            ║"
echo "╚══════════════════════════════════════════════╝"
