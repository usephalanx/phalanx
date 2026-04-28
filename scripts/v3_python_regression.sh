#!/usr/bin/env bash
# Re-validate v3 against the 4 Python testbed cells.
#
# This is the v2_python_regression.sh / record_fixture.sh sibling for v3:
#   1. Reuses cell-config (patch + failing_command + job_name + can_flake)
#   2. Creates a fresh failing branch + PR on the testbed
#   3. Waits for CI to fail (retriggers for flake)
#   4. Waits for the GitHub webhook to dispatch v3's cifix_commander —
#      requires that integration.cifixer_version='v3' for the testbed
#      (the script REFUSES TO RUN if it's still 'v2' to prevent surprises)
#   5. Polls runs.status until terminal (SHIPPED / FAILED / ESCALATED)
#   6. Prints task chain + commit info
#   7. Closes PR + deletes branch
#
# Usage:
#   scripts/v3_python_regression.sh lint
#   scripts/v3_python_regression.sh test_fail
#   scripts/v3_python_regression.sh flake
#   scripts/v3_python_regression.sh coverage
#
# This script does NOT flip cifixer_version — do that manually first:
#   UPDATE ci_integrations SET cifixer_version='v3'
#   WHERE repo_full_name='usephalanx/phalanx-ci-fixer-testbed';
# And flip back to 'v2' when done.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────
SSH_KEY="${SSH_KEY:-$HOME/work/aws/LightsailDefaultKey-us-west-2.pem}"
PROD_HOST="${PROD_HOST:-ubuntu@44.233.157.41}"
PG_CONTAINER="${PG_CONTAINER:-phalanx-prod-postgres-1}"
TESTBED_REPO="${TESTBED_REPO:-usephalanx/phalanx-ci-fixer-testbed}"
TESTBED_LOCAL="${TESTBED_LOCAL:-$HOME/phalanx-ci-fixer-testbed}"
FLAKE_MAX_RETRIGGERS="${FLAKE_MAX_RETRIGGERS:-6}"
INTRO_CI_WAIT_SECS="${INTRO_CI_WAIT_SECS:-360}"
V3_RUN_WAIT_SECS="${V3_RUN_WAIT_SECS:-1200}"   # 20 min — v3 has a bigger DAG

RUN_ID="$(date -u +%Y%m%d-%H%M%S)"

CELL="${1:-}"
[ -z "$CELL" ] && { echo "usage: $0 <lint|test_fail|flake|coverage>"; exit 2; }

# ── Cell config (Python only) ────────────────────────────────────────────
CELL_CONFIG() {
  case "$1" in
    lint)      echo "01-lint-e501.patch|ruff check .|Lint|0" ;;
    test_fail) echo "02-test-assertion.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|0" ;;
    flake)     echo "03-flake-sleep.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|1" ;;
    coverage)  echo "04-coverage-drop.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|0" ;;
    *) echo ""; return 1 ;;
  esac
}

# ── Helpers ──────────────────────────────────────────────────────────────
c_green() { printf "\033[32m%s\033[0m" "$1"; }
c_red()   { printf "\033[31m%s\033[0m" "$1"; }
c_dim()   { printf "\033[2m%s\033[0m"  "$1"; }
c_yel()   { printf "\033[33m%s\033[0m" "$1"; }
say()     { printf "%s\n" "$*"; }

ssh_prod() { ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$PROD_HOST" "$@"; }

pg_query() {
  ssh_prod "docker exec $PG_CONTAINER psql -tA -U forge -d forge -c \"$1\""
}

gh_token() {
  pg_query "SELECT github_token FROM ci_integrations WHERE repo_full_name='$TESTBED_REPO'" | tr -d '\n\r '
}

gh_api() {
  local path="$1"; shift
  curl -s -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com${path}" "$@"
}

wait_ci_conclude() {
  local sha="$1" timeout_s="${2:-360}"
  local deadline=$(( $(date +%s) + timeout_s ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local out
    out=$(gh_api "/repos/$TESTBED_REPO/commits/$sha/check-runs" | python3 -c '
import json, sys
d = json.load(sys.stdin)
runs = d.get("check_runs", [])
latest = {}
for c in runs:
    if c["name"] not in latest or c["id"] > latest[c["name"]]["id"]:
        latest[c["name"]] = c
if not latest: sys.exit(1)
for c in latest.values():
    if not c.get("conclusion"): sys.exit(1)
parts = []
for n, c in latest.items():
    parts.append(n + "=" + str(c["conclusion"]) + ":" + str(c["id"]))
print("|||".join(parts))
' 2>/dev/null || true)
    [ -n "$out" ] && { echo "$out"; return 0; }
    sleep 12
  done
  return 1
}

ci_job_id() {
  local line="$1" name="$2"
  python3 -c "
import sys
line = sys.argv[1]; name = sys.argv[2]
for tok in line.split('|||'):
    if '=' not in tok: continue
    n, rest = tok.split('=', 1)
    if n == name:
        concl, jid = rest.split(':', 1)
        print(concl, jid); break
" "$line" "$name"
}

retrigger_ci() {
  local branch="$1"
  local run_id
  run_id=$(gh_api "/repos/$TESTBED_REPO/actions/runs?branch=$branch&per_page=1" | \
    python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_runs"][0]["id"])' 2>/dev/null)
  [ -z "$run_id" ] && return 1
  curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$TESTBED_REPO/actions/runs/$run_id/rerun" > /dev/null || true
  local sha
  sha=$(gh_api "/repos/$TESTBED_REPO/branches/$branch" | \
    python3 -c 'import json,sys;print(json.load(sys.stdin)["commit"]["sha"])' 2>/dev/null)
  wait_ci_conclude "$sha" 300
}

# Refuse to run if integration isn't on v3 — protects against silent v2 runs.
require_v3_integration() {
  local ver
  ver=$(pg_query "SELECT cifixer_version FROM ci_integrations WHERE repo_full_name='$TESTBED_REPO'" | tr -d '\n\r ')
  if [ "$ver" != "v3" ]; then
    say "$(c_red "FATAL"): testbed cifixer_version=$ver, not v3."
    say "Flip it first:"
    say "  ssh prod 'docker exec $PG_CONTAINER psql -U forge -d forge -c \\"
    say "    \"UPDATE ci_integrations SET cifixer_version='\\''v3'\\'' WHERE repo_full_name='\\''$TESTBED_REPO'\\''\"'"
    exit 3
  fi
  say "$(c_dim "  [pre]") integration cifixer_version=v3 ✓"
}

# Wait for the v3 Run row to appear. The webhook creates a CIFixRun first,
# then v3 dispatch creates a Run via _dispatch_ci_fix_v3. We key off
# work_orders.title which embeds the repo + PR number deterministically.
wait_v3_run_id() {
  local pr_num="$1" deadline=$(( $(date +%s) + 120 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local row
    row=$(pg_query "SELECT r.id FROM runs r JOIN work_orders w ON w.id=r.work_order_id \
      WHERE w.work_order_type='ci_fix' AND w.title LIKE 'Fix CI: $TESTBED_REPO#$pr_num%' \
      ORDER BY r.created_at DESC LIMIT 1" | tr -d '\n\r ')
    [ -n "$row" ] && { echo "$row"; return 0; }
    sleep 5
  done
  return 1
}

wait_v3_terminal() {
  # NOTE: this function is called as `final_status=$(wait_v3_terminal …)`,
  # so progress messages must go to STDERR (>&2) — only the terminal
  # status echoes to stdout. Without the >&2 redirect, every "[run] …
  # status=…" line gets captured into $final_status and the case
  # statement on it never matches.
  local run_id="$1" deadline=$(( $(date +%s) + V3_RUN_WAIT_SECS ))
  local last_status=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local status
    status=$(pg_query "SELECT status FROM runs WHERE id='$run_id'" | tr -d '\n\r ')
    case "$status" in
      SHIPPED|FAILED|ESCALATED)
        echo "$status"; return 0 ;;
    esac
    if [ "$status" != "$last_status" ]; then
      say "$(c_dim "  [run]") $run_id status=$status" >&2
      last_status="$status"
    fi
    sleep 10
  done
  echo "$last_status"
  return 1
}

# ── Per-cell runner ──────────────────────────────────────────────────────
run_cell() {
  local cell="$1"
  local cfg; cfg=$(CELL_CONFIG "$cell") || { say "$(c_red "unknown cell"): $cell"; return 1; }
  IFS='|' read -r patch cmd job_name can_flake <<< "$cfg"

  local branch="v3-rerun/${cell}-${RUN_ID}"

  say ""
  say "━━━ v3 cell: $(c_green "$cell") ━━━ branch: $branch"

  # 1. Create failure branch (mirrors record_fixture.sh)
  (
    cd "$TESTBED_LOCAL"
    git fetch origin --quiet
    git checkout main --quiet
    git pull --ff-only --quiet
    git checkout -B "$branch" main --quiet
    if git apply --check "failures/$patch" 2>/dev/null; then
      git apply "failures/$patch"
    elif [ "$patch" = "04-coverage-drop.patch" ]; then
      python3 -c "
from pathlib import Path
p = Path('src/calc/math_ops.py')
txt = p.read_text().rstrip() + '''


def percentage(part: float, whole: float) -> float:
    \"\"\"Return part as a percentage of whole (0-100).\"\"\"
    if whole == 0:
        raise ZeroDivisionError('cannot compute percentage of zero')
    return (part / whole) * 100


def average(values: list[float]) -> float:
    \"\"\"Return the arithmetic mean of a non-empty list.\"\"\"
    if not values:
        raise ValueError('cannot average an empty list')
    return sum(values) / len(values)
'''
p.write_text(txt)
"
    else
      say "$(c_red "apply_patch failed"): $patch"; return 1
    fi
    git add -A
    git -c user.name="record-bot" -c user.email="bot@phalanx.local" \
        commit -m "v3-rerun/$cell: intro failure" --quiet
    git push -u origin "$branch" --quiet
  )

  local intro_sha
  intro_sha=$(cd "$TESTBED_LOCAL" && git rev-parse "$branch")
  say "$(c_dim "  [setup]") intro commit=$intro_sha"

  # 2. Open PR
  local pr_resp pr_num
  pr_resp=$(curl -s -X POST \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$TESTBED_REPO/pulls" \
    -d "{\"title\":\"v3-rerun/${cell} [$RUN_ID]\",\"head\":\"$branch\",\"base\":\"main\"}")
  pr_num=$(echo "$pr_resp" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("number") or "")' 2>/dev/null)
  [ -z "$pr_num" ] && { say "$(c_red "PR create failed"): $pr_resp" | head -3; return 1; }
  say "$(c_dim "  [setup]") opened PR #$pr_num"

  # 3. Wait for CI to fail
  local ci_line job_id="" concl=""
  for attempt in $(seq 1 "$FLAKE_MAX_RETRIGGERS"); do
    ci_line=$(wait_ci_conclude "$intro_sha" "$INTRO_CI_WAIT_SECS") || {
      say "$(c_red "  [ci]") timeout"; return 1
    }
    say "$(c_dim "  [ci]") $ci_line"
    read -r concl job_id < <(ci_job_id "$ci_line" "$job_name")
    [ "$concl" = "failure" ] && break
    if [ "$can_flake" = "1" ] && [ "$attempt" -lt "$FLAKE_MAX_RETRIGGERS" ]; then
      say "$(c_dim "  [ci]") retrigger (flake rolled green)"
      ci_line=$(retrigger_ci "$branch") || true
    else
      say "$(c_red "  [ci]") $job_name did not fail"; return 1
    fi
  done
  say "$(c_dim "  [ci]") failing job $job_name id=$job_id"

  # 4. Wait for v3 Run row (webhook should have fired by now)
  say "$(c_dim "  [v3]") waiting for cifix_commander dispatch…"
  local v3_run_id
  v3_run_id=$(wait_v3_run_id "$pr_num") || {
    say "$(c_red "  [v3]") no Run row appeared in 120s — webhook didn't dispatch"
    return 1
  }
  say "$(c_dim "  [v3]") run_id=$v3_run_id"

  # 5. Poll to terminal status
  say "$(c_dim "  [v3]") polling to terminal status (cap=${V3_RUN_WAIT_SECS}s)…"
  local final_status
  final_status=$(wait_v3_terminal "$v3_run_id") || {
    say "$(c_red "  [v3]") timeout — last status: $final_status"
    return 1
  }

  # 6. Inspect outcome
  case "$final_status" in
    SHIPPED) say "$(c_green "  [verdict]") SHIPPED ✓" ;;
    FAILED) say "$(c_red "  [verdict]") FAILED ✗" ;;
    ESCALATED) say "$(c_yel "  [verdict]") ESCALATED ⚠" ;;
  esac

  # Tasks table has no `task_type` — use sequence_num + agent_role + a
  # short title slice. Sort by sequence_num so iter-1 tasks come before
  # iter-2 follow-ups in the output.
  say "$(c_dim "  [tasks]")"
  pg_query "SELECT sequence_num, agent_role, status, \
            COALESCE(EXTRACT(EPOCH FROM (completed_at - started_at))::int, 0) AS dur_s, \
            LEFT(title, 70) AS title_short, COALESCE(error, '') AS err \
            FROM tasks WHERE run_id='$v3_run_id' ORDER BY sequence_num" \
    | awk -F'|' '{printf "    [%s] %-15s %-10s %5ss  %s%s\n", $1, $2, $3, $4, $5, ($6 == "" ? "" : "  ERR:" $6)}'

  # PR HEAD sha (did v3 push a fix?)
  local head_sha
  head_sha=$(gh_api "/repos/$TESTBED_REPO/branches/$branch" | \
    python3 -c 'import json,sys;print(json.load(sys.stdin).get("commit",{}).get("sha",""))' 2>/dev/null || echo "")
  if [ -n "$head_sha" ] && [ "$head_sha" != "$intro_sha" ]; then
    say "$(c_green "  [commit]") $intro_sha → $head_sha"
  else
    say "$(c_dim "  [commit]") no commit pushed (HEAD still $intro_sha)"
  fi

  # 7. Cleanup PR + branch
  say "$(c_dim "  [cleanup]") closing PR #$pr_num + deleting branch"
  curl -s -X PATCH -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$TESTBED_REPO/pulls/$pr_num" \
    -d '{"state":"closed"}' > /dev/null || true
  curl -s -X DELETE -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$TESTBED_REPO/git/refs/heads/$branch" > /dev/null || true

  echo "$cell|$final_status|$v3_run_id|$intro_sha|${head_sha:-}"
}

# ── Main ────────────────────────────────────────────────────────────────
GH_TOKEN="$(gh_token)"
[ -z "$GH_TOKEN" ] && { say "$(c_red "fatal"): no github token"; exit 2; }
export GH_TOKEN

require_v3_integration

run_cell "$CELL"
