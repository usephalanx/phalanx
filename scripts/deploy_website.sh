#!/bin/bash
# Deploy the static website (usephalanx-website repo) to prod + tag.
#
# Mirrors the shape of ../deploy.sh: rsync the site dir into the nginx
# bind-mount on prod, auto-bump a site-vMAJOR.MINOR.PATCH tag in the
# website repo, push the tag. No container restart needed — nginx
# picks up new files from the read-only bind mount instantly.
#
# Usage:
#   scripts/deploy_website.sh                   # auto-bump patch
#   scripts/deploy_website.sh site-v1.3.0       # explicit tag
#   scripts/deploy_website.sh --dry-run         # rsync --dry-run + skip tag
#
# Required env (or edit defaults below):
#   DEPLOY_HOST   user@server-ip (default: ubuntu@44.233.157.41)
#   DEPLOY_KEY    path to SSH key (falls back through common locations)
#   SITE_DIR      local website repo path (default: ~/usephalanx-website)
#   SITE_REMOTE_DIR  remote site dir mounted into nginx
#                    (default: /home/ubuntu/phalanx/site)

set -euo pipefail

SITE_DIR="${SITE_DIR:-$HOME/usephalanx-website}"
SITE_REMOTE_DIR="${SITE_REMOTE_DIR:-/home/ubuntu/phalanx/site}"
SERVER="${DEPLOY_HOST:-ubuntu@44.233.157.41}"

# SSH key resolution chain (same as deploy.sh)
if [ -n "${DEPLOY_KEY:-}" ] && [ -f "$DEPLOY_KEY" ]; then
  SSH_KEY="$DEPLOY_KEY"
elif [ -f "$HOME/work/aws/LightsailDefaultKey-us-west-2.pem" ]; then
  SSH_KEY="$HOME/work/aws/LightsailDefaultKey-us-west-2.pem"
elif [ -f "$HOME/work/LightsailDefaultKey-us-west-2.pem" ]; then
  SSH_KEY="$HOME/work/LightsailDefaultKey-us-west-2.pem"
elif [ -f "$HOME/.ssh/id_rsa" ]; then
  SSH_KEY="$HOME/.ssh/id_rsa"
elif [ -f "$HOME/.ssh/id_ed25519" ]; then
  SSH_KEY="$HOME/.ssh/id_ed25519"
else
  echo "ERROR: SSH key not found. Set DEPLOY_KEY env var." >&2
  exit 1
fi

if [ ! -d "$SITE_DIR/.git" ]; then
  echo "ERROR: $SITE_DIR is not a git repo." >&2
  exit 1
fi

DRY_RUN=0
EXPLICIT_TAG=""
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") : ;;
  *) EXPLICIT_TAG="$1" ;;
esac

# ── Compute next tag ──────────────────────────────────────────────────────────
cd "$SITE_DIR"
if [ -n "$EXPLICIT_TAG" ]; then
  RELEASE_TAG="$EXPLICIT_TAG"
else
  LAST_TAG=$(git tag --list 'site-v*' --sort=-v:refname | head -n 1)
  if [ -z "$LAST_TAG" ]; then
    RELEASE_TAG="site-v1.0.0"
  else
    # bump patch: site-v1.2.3 → site-v1.2.4
    RELEASE_TAG=$(echo "$LAST_TAG" | awk -F. '{printf "%s.%s.%d", $1, $2, $3+1}')
  fi
fi

echo "╔══════════════════════════════════════════════╗"
echo "║  Phalanx Website Deploy"
echo "║  Release : $RELEASE_TAG"
echo "║  Target  : $SERVER:$SITE_REMOTE_DIR"
echo "║  Source  : $SITE_DIR"
echo "║  Dry-run : $([ "$DRY_RUN" -eq 1 ] && echo yes || echo no)"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Rsync ─────────────────────────────────────────────────────────────────────
RSYNC_FLAGS="-avz --delete"
[ "$DRY_RUN" -eq 1 ] && RSYNC_FLAGS="$RSYNC_FLAGS --dry-run"

# Preserve prod's auto-regenerated sitemap.xml; skip meta files.
rsync $RSYNC_FLAGS \
  --exclude='.git' --exclude='README.md' --exclude='sitemap.xml' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  "$SITE_DIR/" "$SERVER:$SITE_REMOTE_DIR/"

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "Dry-run complete — no files transferred, no tag created."
  exit 0
fi

# ── Tag + push ────────────────────────────────────────────────────────────────
cd "$SITE_DIR"
if git tag --list | grep -qx "$RELEASE_TAG"; then
  echo "Tag $RELEASE_TAG already exists — skipping tag creation."
else
  LAST_SHIPPED_TAG=$(git tag --list 'site-v*' --sort=-v:refname | head -n 1 || true)
  if [ -n "$LAST_SHIPPED_TAG" ]; then
    NOTES=$(git log "$LAST_SHIPPED_TAG"..HEAD --pretty=format:"- %s" --no-merges 2>/dev/null || echo "- Site update")
  else
    NOTES=$(git log --pretty=format:"- %s" --no-merges -10 2>/dev/null || echo "- Initial release")
  fi
  git tag -a "$RELEASE_TAG" -m "$(cat <<EOF
$RELEASE_TAG — $(date +%Y-%m-%d)

$NOTES
EOF
)"
  echo "✓ Tagged $RELEASE_TAG"
fi

git push origin "$RELEASE_TAG" 2>&1 | tail -3 || true

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Website deploy complete: $RELEASE_TAG"
echo "╚══════════════════════════════════════════════╝"
