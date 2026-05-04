#!/usr/bin/env bash
# Phase 3 canary — humanize intword decimal-separator locale fix re-derivation.
#
# Premise: humanize commit a47a89e ("fix: handle tz-aware datetimes in
# naturalday and naturaldate") was a real maintainer-authored fix. The
# commit modified src/humanize/time.py + added tests in tests/test_time.py.
#
# This script reverts ONLY the src/humanize/time.py portion on a fresh
# branch, keeping the test additions in place. Push as PR. The kept tests
# fail without the src fix — real CI red on humanize. v3 dispatches.
#
# Acceptance (manual review at end):
#   1. v3 reaches SHIPPED
#   2. v3 commits a fix to src/humanize/time.py
#   3. Real GitHub CI on v3's commit goes green
#   4. v3's diff addresses tz-aware datetime handling
#      (compare to original a47a89e — exact match or functional equivalent)

set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/work/aws/LightsailDefaultKey-us-west-2.pem}"
PROD_HOST="${PROD_HOST:-ubuntu@44.233.157.41}"
PG_CONTAINER="${PG_CONTAINER:-phalanx-prod-postgres-1}"
REPO="usephalanx/humanize"
LOCAL_DIR="${LOCAL_DIR:-/tmp/humanize-regress}"
TARGET_COMMIT="${TARGET_COMMIT:-7175184}"
TARGET_FILE="${TARGET_FILE:-src/humanize/number.py}"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
BRANCH="path2/intword-revert-${RUN_ID}"
V3_DISPATCH_WAIT_SECS="${V3_DISPATCH_WAIT_SECS:-600}"
V3_RUN_WAIT_SECS="${V3_RUN_WAIT_SECS:-1800}"  # 30min — Path 1 may iter

ssh_prod() { ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$PROD_HOST" "$@"; }
pg_query() { ssh_prod "docker exec $PG_CONTAINER psql -tA -U forge -d forge -c \"$1\""; }
GH_TOKEN="$(pg_query "SELECT github_token FROM ci_integrations WHERE repo_full_name='$REPO'" | tr -d '\n\r ')"
[ -z "$GH_TOKEN" ] && { echo "no token for $REPO"; exit 2; }

ver=$(pg_query "SELECT cifixer_version FROM ci_integrations WHERE repo_full_name='$REPO'" | tr -d '\n\r ')
[ "$ver" != "v3" ] && { echo "$REPO is on $ver, not v3"; exit 3; }
echo "  [pre] $REPO cifixer_version=v3 ✓"
echo "  [pre] target commit=$TARGET_COMMIT  (revert in $TARGET_FILE only)"

echo "━━━ Path 2: $BRANCH"

(
  cd "$LOCAL_DIR"
  git fetch origin --quiet
  git checkout main --quiet
  git pull --ff-only --quiet
  git checkout -B "$BRANCH" main --quiet

  # Reverse-apply ONLY the src/humanize/time.py portion of $TARGET_COMMIT.
  # `git show <commit> -- <path>` prints the diff for that path; pipe to
  # `git apply --reverse` to undo it on the working tree without touching
  # tests/test_time.py.
  git show "$TARGET_COMMIT" -- "$TARGET_FILE" | git apply --reverse

  # Verify only $TARGET_FILE is dirty.
  if [ "$(git status --porcelain | grep -c '^.M\|^.A')" -ne 1 ]; then
    echo "FATAL: revert touched unexpected files"
    git status --porcelain
    exit 1
  fi

  git add "$TARGET_FILE"
  git -c user.name="record-bot" -c user.email="bot@phalanx.local" \
      commit -m "path2: revert ${TARGET_COMMIT}'s ${TARGET_FILE} to test v3 re-derivation" --quiet
  git push -u origin "$BRANCH" --quiet
)

INTRO_SHA=$(cd "$LOCAL_DIR" && git rev-parse "$BRANCH")
echo "  [setup] revert commit=$INTRO_SHA"

PR_RESP=$(curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/pulls" \
  -d "{\"title\":\"path2: revert intword-locale fix to test v3 re-derivation [$RUN_ID]\",\"head\":\"$BRANCH\",\"base\":\"main\",\"body\":\"Phalanx v3 wild-bug proof. Revert of $TARGET_COMMIT (src only). Bot will close after observation.\"}")
PR_NUM=$(echo "$PR_RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("number") or "")' 2>/dev/null)
[ -z "$PR_NUM" ] && { echo "PR fail: $(echo "$PR_RESP" | head -c 200)"; exit 1; }
echo "  [setup] opened PR #$PR_NUM"

curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/$REPO/issues/$PR_NUM/labels" \
  -d '{"labels":["changelog: skip"]}' > /dev/null && echo "  [setup] label added"

cleanup() {
  echo "  [cleanup] closing PR #$PR_NUM + deleting branch"
  curl -s -X PATCH -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$REPO/pulls/$PR_NUM" -d '{"state":"closed"}' > /dev/null || true
  curl -s -X DELETE -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$REPO/git/refs/heads/$BRANCH" > /dev/null || true
}
trap cleanup EXIT

echo "  [v3] waiting for cifix_commander dispatch (cap=${V3_DISPATCH_WAIT_SECS}s)…"
v3_run_id=""
deadline=$(( $(date +%s) + V3_DISPATCH_WAIT_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  v3_run_id=$(pg_query "SELECT r.id FROM runs r JOIN work_orders w ON w.id=r.work_order_id \
    WHERE w.work_order_type='ci_fix' AND w.title LIKE 'Fix CI: $REPO#$PR_NUM%' \
    ORDER BY r.created_at DESC LIMIT 1" | tr -d '\n\r ')
  [ -n "$v3_run_id" ] && break
  sleep 10
done
[ -z "$v3_run_id" ] && { echo "no dispatch within ${V3_DISPATCH_WAIT_SECS}s"; exit 1; }
echo "  [v3] run=$v3_run_id"

deadline=$(( $(date +%s) + V3_RUN_WAIT_SECS ))
last=""
final=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  st=$(pg_query "SELECT status FROM runs WHERE id='$v3_run_id'" | tr -d '\n\r ')
  case "$st" in SHIPPED|FAILED|ESCALATED) final="$st"; break ;; esac
  if [ "$st" != "$last" ]; then echo "  [run] status=$st"; last="$st"; fi
  sleep 10
done
[ -z "$final" ] && { echo "  [v3] timeout last=$last"; exit 1; }
echo "  [verdict] $final"

echo "  [tasks]"
pg_query "SELECT sequence_num, agent_role, status, COALESCE(EXTRACT(EPOCH FROM (completed_at - started_at))::int,0) AS dur_s, LEFT(title,55), COALESCE(error,'') FROM tasks WHERE run_id='$v3_run_id' ORDER BY sequence_num" \
  | awk -F'|' '{printf "    [%s] %-15s %-10s %5ss  %s%s\n", $1, $2, $3, $4, $5, ($6==""?"":"  ERR:"$6)}'

HEAD_SHA=$(curl -s -H "Authorization: Bearer $GH_TOKEN" "https://api.github.com/repos/$REPO/branches/$BRANCH" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("commit",{}).get("sha",""))' 2>/dev/null || echo "")
if [ -n "$HEAD_SHA" ] && [ "$HEAD_SHA" != "$INTRO_SHA" ]; then
  echo "  [commit] $INTRO_SHA → $HEAD_SHA"
  COMMIT_FILES=$(curl -s -H "Authorization: Bearer $GH_TOKEN" "https://api.github.com/repos/$REPO/commits/$HEAD_SHA" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(", ".join(f["filename"] for f in d.get("files",[])))' 2>/dev/null)
  echo "  [commit-files] $COMMIT_FILES"
  echo "  [diff vs original $TARGET_COMMIT — manual review required]"
  echo "  [v3 commit:]"
  curl -s -H "Authorization: Bearer $GH_TOKEN" "https://api.github.com/repos/$REPO/commits/$HEAD_SHA" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for f in d.get('files', []):
    if f['filename'] == '$TARGET_FILE':
        print(f.get('patch', '(no patch field)')[:2000])
"
else
  echo "  [commit] none — v3 didn't push a fix"
fi

echo "humanize-path1|$final|$v3_run_id|$INTRO_SHA|${HEAD_SHA:-}"
