#!/usr/bin/env bash
# v2 Python regression smoke
# ─────────────────────────
# Re-validates all 4 closed Python scorecard cells on prod (lint,
# test_fail, flake, coverage) against the real agent + real sandbox
# + real GitHub CI.
#
# This is the Layer 2 gate from the TypeScript regression plan: run
# before any deploy that touches shared ci_fixer_v2 code. If a TS
# (or other language) change silently breaks one of the Python
# cells, this script catches it in ~15–25 min instead of at prod.
#
# Cost: ~$1–2 per run (one simulate per cell), ~$0 if Layer 3
# (replay) replaces it later.
#
# Each cell:
#   1. Creates a fresh branch off testbed main
#   2. Applies the failure-introduction patch
#   3. Opens a PR (so Actions runs)
#   4. Waits for CI to fail — retriggers up to N times for flake
#   5. Runs simulate via ssh+docker exec on prod
#   6. Waits for CI on the agent's fix-commit
#   7. Asserts: verdict=committed AND all CI checks success
#
# Usage:
#   ./scripts/v2_python_regression.sh                  # all 4 cells
#   ./scripts/v2_python_regression.sh --cell=lint      # single cell
#   ./scripts/v2_python_regression.sh --cleanup        # delete branches on exit
#   ./scripts/v2_python_regression.sh --baseline       # no fail-if-over-budget; record only
#
# Env overrides:
#   SSH_KEY, PROD_HOST, CONTAINER, TESTBED_REPO, TESTBED_LOCAL
#   PG_CONTAINER, PER_CELL_MAX_COST_USD, PER_CELL_MAX_WALL_SECS
#   FLAKE_MAX_RETRIGGERS
#
# Exit codes:
#   0   all requested cells PASS
#   1   at least one cell FAIL (regression)
#   2   setup / env error before any cell ran

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────
SSH_KEY="${SSH_KEY:-$HOME/work/aws/LightsailDefaultKey-us-west-2.pem}"
PROD_HOST="${PROD_HOST:-ubuntu@44.233.157.41}"
CONTAINER="${CONTAINER:-phalanx-prod-phalanx-ci-fixer-worker-1}"
PG_CONTAINER="${PG_CONTAINER:-phalanx-prod-postgres-1}"
TESTBED_REPO="${TESTBED_REPO:-usephalanx/phalanx-ci-fixer-testbed}"
TESTBED_LOCAL="${TESTBED_LOCAL:-$HOME/phalanx-ci-fixer-testbed}"
PER_CELL_MAX_COST_USD="${PER_CELL_MAX_COST_USD:-1.50}"
PER_CELL_MAX_WALL_SECS="${PER_CELL_MAX_WALL_SECS:-600}"
FLAKE_MAX_RETRIGGERS="${FLAKE_MAX_RETRIGGERS:-6}"

RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
LOG_DIR="/tmp/v2-py-regression-${RUN_ID}"
mkdir -p "$LOG_DIR"

BASELINE=0
CLEANUP=0
REQUEST_CELL=""

for arg in "$@"; do
  case "$arg" in
    --baseline) BASELINE=1 ;;
    --cleanup)  CLEANUP=1 ;;
    --cell=*)   REQUEST_CELL="${arg#--cell=}" ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

# ── Cells ────────────────────────────────────────────────────────────────
# name | patch | failing_command | failing_job_name | can_flake
CELLS=(
  "lint|01-lint-e501.patch|ruff check .|Lint|0"
  "test_fail|02-test-assertion.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|0"
  "flake|03-flake-sleep.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|1"
  "coverage|04-coverage-drop.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|0"
)

# ── Helpers ──────────────────────────────────────────────────────────────
c_red()   { printf "\033[31m%s\033[0m" "$1"; }
c_green() { printf "\033[32m%s\033[0m" "$1"; }
c_dim()   { printf "\033[2m%s\033[0m"  "$1"; }

say() { printf "%s\n" "$*"; }

ssh_prod() {
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$PROD_HOST" "$@"
}

gh_token() {
  ssh_prod "docker exec $PG_CONTAINER psql -tA -U forge -d forge -c \
    \"SELECT github_token FROM ci_integrations WHERE repo_full_name='$TESTBED_REPO'\"" | tr -d '\n\r '
}

gh_api() {
  local path="$1"; shift
  curl -s -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com${path}" "$@"
}

# Wait for CI on a sha to conclude. Echos "jobA=conclusion:id jobB=conclusion:id"
# Returns 0 if all jobs conclude, 1 if timeout.
wait_ci_conclude() {
  local sha="$1"
  local timeout_s="${2:-180}"
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
if not latest or not all(c.get("conclusion") for c in latest.values()):
    sys.exit(1)
print(" ".join(f"{n}={c[\"conclusion\"]}:{c[\"id\"]}" for n, c in latest.items()))
' 2>/dev/null || true)
    if [ -n "$out" ]; then
      echo "$out"
      return 0
    fi
    sleep 12
  done
  return 1
}

# Extract one job's id + conclusion from a wait_ci_conclude line
ci_job_conclusion() {
  local line="$1" name="$2"
  python3 -c "
line = '''$line'''
name = '''$name'''
for tok in line.split():
    n, rest = tok.split('=', 1)
    if n == name:
        concl, jid = rest.split(':', 1)
        print(concl, jid)
        break
"
}

# Retrigger the latest workflow run for a branch; block until conclusion.
# Echo conclusion line.
retrigger_ci() {
  local branch="$1"
  local run_id
  run_id=$(gh_api "/repos/$TESTBED_REPO/actions/runs?branch=$branch&per_page=1" | \
    python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_runs"][0]["id"])')
  curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$TESTBED_REPO/actions/runs/$run_id/rerun" > /dev/null || true
  # Find the HEAD sha of the branch so wait_ci_conclude has something to poll
  local sha
  sha=$(gh_api "/repos/$TESTBED_REPO/branches/$branch" | python3 -c 'import json,sys;print(json.load(sys.stdin)["commit"]["sha"])')
  wait_ci_conclude "$sha" 240
}

# Run simulate on prod. Echoes: verdict cost_usd tokens fix_sha
run_simulate() {
  local repo="$1" branch="$2" sha="$3" job_id="$4" cmd="$5" job_name="$6"
  ssh_prod "docker exec $CONTAINER python -m phalanx.ci_fixer_v2.simulate \
    --repo $repo --pr 0 \
    --branch $branch --sha $sha \
    --job-id $job_id \
    --failing-command $(printf %q "$cmd") \
    --failing-job-name $(printf %q "$job_name")" 2>&1 | python3 -c '
import re, sys
text = sys.stdin.read()
verdict = "unknown"
cost = 0.0
tokens = 0
fix_sha = ""
for line in text.splitlines():
    m = re.search(r"Verdict:\s+(\w+)", line)
    if m: verdict = m.group(1).lower()
    m = re.search(r"Commit SHA:\s+([0-9a-f]+)", line)
    if m: fix_sha = m.group(1)
    m = re.search(r"total_cost_usd:\s+\$?([0-9.]+)", line)
    if m: cost = float(m.group(1))
    m = re.search(r"tokens_used:\s+(\d+)", line)
    if m: tokens = int(m.group(1))
print(f"{verdict} {cost} {tokens} {fix_sha}")
' | tr -d '\r'
}

# ── Preflight ────────────────────────────────────────────────────────────
say "$(c_dim "[preflight]") run_id=$RUN_ID log_dir=$LOG_DIR"
say "$(c_dim "[preflight]") testbed=$TESTBED_REPO container=$CONTAINER"
say "$(c_dim "[preflight]") budgets: max_cost=\$$PER_CELL_MAX_COST_USD max_wall=${PER_CELL_MAX_WALL_SECS}s"
[ "$BASELINE" = "1" ] && say "$(c_dim "[preflight]") BASELINE mode: budgets are recorded, not enforced"

if [ ! -f "$SSH_KEY" ]; then
  say "$(c_red "fatal"): SSH_KEY not found: $SSH_KEY"
  exit 2
fi
if [ ! -d "$TESTBED_LOCAL" ]; then
  say "$(c_red "fatal"): TESTBED_LOCAL not found: $TESTBED_LOCAL"
  exit 2
fi

GH_TOKEN="$(gh_token)"
if [ -z "$GH_TOKEN" ] || [ "${#GH_TOKEN}" -lt 20 ]; then
  say "$(c_red "fatal"): could not fetch GitHub token from prod DB"
  exit 2
fi
export GH_TOKEN

# Sync testbed to latest main
(
  cd "$TESTBED_LOCAL"
  git fetch origin --prune --quiet
  git checkout main --quiet
  git pull --ff-only --quiet
) || { say "$(c_red "fatal"): testbed sync failed"; exit 2; }

MAIN_SHA=$(cd "$TESTBED_LOCAL" && git rev-parse main)
say "$(c_dim "[preflight]") testbed main @ $MAIN_SHA"

# ── Per-cell runner ──────────────────────────────────────────────────────
CREATED_BRANCHES=()
declare -A RESULTS_VERDICT RESULTS_COST RESULTS_TOKENS RESULTS_FIX_SHA RESULTS_WALL RESULTS_CI_AFTER RESULTS_STATUS

run_cell() {
  local name="$1" patch="$2" cmd="$3" job_name="$4" can_flake="$5"
  local branch="regression/${name}-${RUN_ID}"
  local cell_log="${LOG_DIR}/${name}.log"
  local started=$(date +%s)

  say ""
  say "━━━ cell: $(c_green "$name") ━━━ branch: $branch"
  CREATED_BRANCHES+=("$branch")

  # Create branch + apply patch
  (
    cd "$TESTBED_LOCAL"
    git checkout -B "$branch" main --quiet
    if ! git apply --check "failures/$patch" 2>/dev/null; then
      # Fallback: some patches have trailing-newline quirks; try with --recount
      git apply --recount "failures/$patch" 2>&1 | tee -a "$cell_log" >&2 || {
        say "$(c_red "  apply_patch failed"): $patch (see $cell_log)"
        return 11
      }
    else
      git apply "failures/$patch"
    fi
    git add -A
    git -c user.name="regression-bot" -c user.email="bot@phalanx.local" \
        commit -m "regression/$name: intro failure ($patch)" --quiet
    git push -u origin "$branch" --quiet 2>&1 | tee -a "$cell_log" >/dev/null
  ) || return 11

  local intro_sha
  intro_sha=$(cd "$TESTBED_LOCAL" && git rev-parse "$branch")
  say "$(c_dim "  [setup]") intro commit=$intro_sha"

  # Wait for CI to conclude, retrigger for flake
  local ci_conclusion job_id=""
  for attempt in $(seq 1 "$FLAKE_MAX_RETRIGGERS"); do
    if ! ci_conclusion=$(wait_ci_conclude "$intro_sha" 240); then
      say "$(c_red "  [ci-intro]") timeout waiting for CI"
      return 12
    fi
    say "$(c_dim "  [ci-intro]") $ci_conclusion"
    read -r job_concl job_id < <(ci_job_conclusion "$ci_conclusion" "$job_name")
    if [ "$job_concl" = "failure" ]; then
      break
    fi
    if [ "$can_flake" = "1" ] && [ "$attempt" -lt "$FLAKE_MAX_RETRIGGERS" ]; then
      say "$(c_dim "  [ci-intro]") flake rolled green; retrigger $attempt/$FLAKE_MAX_RETRIGGERS"
      ci_conclusion=$(retrigger_ci "$branch") || true
    else
      say "$(c_red "  [ci-intro]") $job_name did not fail on intro commit"
      return 13
    fi
  done
  say "$(c_dim "  [ci-intro]") failing job $job_name id=$job_id"

  # Run simulate on prod
  say "$(c_dim "  [simulate]") starting…"
  local sim_out
  sim_out=$(run_simulate "$TESTBED_REPO" "$branch" "$intro_sha" "$job_id" "$cmd" "$job_name" | tee -a "$cell_log")
  read -r verdict cost tokens fix_sha <<< "$sim_out"
  say "$(c_dim "  [simulate]") verdict=$verdict cost=\$$cost tokens=$tokens fix=$fix_sha"
  RESULTS_VERDICT[$name]=$verdict
  RESULTS_COST[$name]=$cost
  RESULTS_TOKENS[$name]=$tokens
  RESULTS_FIX_SHA[$name]=$fix_sha

  if [ "$verdict" != "committed" ]; then
    say "$(c_red "  [REGRESSION]") expected committed, got $verdict"
    RESULTS_CI_AFTER[$name]="-"
    RESULTS_WALL[$name]=$(( $(date +%s) - started ))
    RESULTS_STATUS[$name]="FAIL"
    return 14
  fi

  # Wait for CI on the fix
  local fix_ci
  if ! fix_ci=$(wait_ci_conclude "$fix_sha" 240); then
    say "$(c_red "  [ci-fix]") timeout waiting for agent's fix CI"
    RESULTS_CI_AFTER[$name]="timeout"
    RESULTS_STATUS[$name]="FAIL"
    return 15
  fi
  say "$(c_dim "  [ci-fix]") $fix_ci"
  RESULTS_CI_AFTER[$name]="$fix_ci"

  # Assert every CI job is success
  local any_red=0
  for kv in $fix_ci; do
    local concl="${kv#*=}"
    concl="${concl%%:*}"
    if [ "$concl" != "success" ]; then
      any_red=1
    fi
  done
  RESULTS_WALL[$name]=$(( $(date +%s) - started ))
  if [ "$any_red" = "1" ]; then
    say "$(c_red "  [REGRESSION]") agent's fix pushed but CI did not go fully green"
    RESULTS_STATUS[$name]="FAIL"
    return 16
  fi

  say "$(c_green "  [PASS]") ${name} (${RESULTS_WALL[$name]}s, \$$cost)"
  RESULTS_STATUS[$name]="PASS"
  return 0
}

# ── Run cells ────────────────────────────────────────────────────────────
OVERALL_RC=0

for row in "${CELLS[@]}"; do
  IFS='|' read -r name patch cmd job_name can_flake <<< "$row"
  if [ -n "$REQUEST_CELL" ] && [ "$REQUEST_CELL" != "$name" ]; then
    continue
  fi
  set +e
  run_cell "$name" "$patch" "$cmd" "$job_name" "$can_flake"
  rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    OVERALL_RC=1
  fi
done

# ── Summary ──────────────────────────────────────────────────────────────
say ""
say "━━━ summary — run $RUN_ID ━━━"
printf "  %-10s  %-6s  %-8s  %-8s  %-6s  %s\n" "cell" "status" "verdict" "cost_usd" "wall_s" "ci_after"
printf "  %-10s  %-6s  %-8s  %-8s  %-6s  %s\n" "----" "------" "-------" "--------" "------" "--------"
for row in "${CELLS[@]}"; do
  IFS='|' read -r name _ _ _ _ <<< "$row"
  if [ -n "$REQUEST_CELL" ] && [ "$REQUEST_CELL" != "$name" ]; then
    continue
  fi
  local_status="${RESULTS_STATUS[$name]:-SKIP}"
  local_verdict="${RESULTS_VERDICT[$name]:--}"
  local_cost="${RESULTS_COST[$name]:--}"
  local_wall="${RESULTS_WALL[$name]:--}"
  local_ci="${RESULTS_CI_AFTER[$name]:--}"
  printf "  %-10s  %-6s  %-8s  %-8s  %-6s  %s\n" \
    "$name" "$local_status" "$local_verdict" "$local_cost" "$local_wall" "$local_ci"
done

# Budget enforcement (off in --baseline)
if [ "$BASELINE" = "0" ]; then
  for row in "${CELLS[@]}"; do
    IFS='|' read -r name _ _ _ _ <<< "$row"
    [ -n "$REQUEST_CELL" ] && [ "$REQUEST_CELL" != "$name" ] && continue
    [ "${RESULTS_STATUS[$name]:-}" != "PASS" ] && continue
    cost="${RESULTS_COST[$name]}"
    wall="${RESULTS_WALL[$name]}"
    if python3 -c "import sys; sys.exit(0 if float('$cost') > float('$PER_CELL_MAX_COST_USD') else 1)" 2>/dev/null; then
      say "$(c_red "BUDGET"): $name cost \$$cost > \$$PER_CELL_MAX_COST_USD"
      OVERALL_RC=1
    fi
    if [ "$wall" -gt "$PER_CELL_MAX_WALL_SECS" ]; then
      say "$(c_red "BUDGET"): $name wall ${wall}s > ${PER_CELL_MAX_WALL_SECS}s"
      OVERALL_RC=1
    fi
  done
fi

# ── Cleanup ──────────────────────────────────────────────────────────────
if [ "$CLEANUP" = "1" ]; then
  for b in "${CREATED_BRANCHES[@]}"; do
    say "$(c_dim "[cleanup]") delete $b"
    curl -s -X DELETE -H "Authorization: Bearer $GH_TOKEN" \
      "https://api.github.com/repos/$TESTBED_REPO/git/refs/heads/$b" > /dev/null || true
  done
else
  say ""
  say "$(c_dim "[cleanup]") branches preserved for diagnosis:"
  for b in "${CREATED_BRANCHES[@]}"; do say "  $b"; done
  say "$(c_dim "[cleanup]") rerun with --cleanup to delete"
fi

if [ $OVERALL_RC -eq 0 ]; then
  say ""
  say "$(c_green "ALL CELLS PASS") — safe to deploy shared v2 changes."
else
  say ""
  say "$(c_red "REGRESSION DETECTED") — do NOT deploy. Logs: $LOG_DIR"
fi

exit $OVERALL_RC
