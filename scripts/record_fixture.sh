#!/usr/bin/env bash
# Record a single replay fixture by running a live simulate end-to-end
# on prod. Used as a one-time investment per scorecard cell.
#
# Pattern mirrors scripts/v2_python_regression.sh:
#   1. create a fresh branch off testbed main, apply failure patch
#   2. open a PR so CI fires
#   3. wait for CI to fail (retrigger for flake)
#   4. invoke simulate --record INSIDE the prod container (writes
#      fixture to /tmp inside container)
#   5. docker cp the fixture out of the container, scp to this laptop,
#      place under tests/fixtures/scorecard/<lang>/<cell>.json
#   6. clean up branch + PR
#
# Usage:
#   scripts/record_fixture.sh lint           # one cell
#   scripts/record_fixture.sh test_fail      # another
#   scripts/record_fixture.sh flake
#   scripts/record_fixture.sh coverage
#   scripts/record_fixture.sh all            # all 4 sequentially

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────
SSH_KEY="${SSH_KEY:-$HOME/work/aws/LightsailDefaultKey-us-west-2.pem}"
PROD_HOST="${PROD_HOST:-ubuntu@44.233.157.41}"
CONTAINER="${CONTAINER:-phalanx-prod-phalanx-ci-fixer-worker-1}"
PG_CONTAINER="${PG_CONTAINER:-phalanx-prod-postgres-1}"
# Language default: python (back-compat). Override with LANG=ts.
LANG_ROW="${LANG_ROW:-python}"
case "$LANG_ROW" in
  python)
    TESTBED_REPO_DEFAULT="usephalanx/phalanx-ci-fixer-testbed"
    TESTBED_LOCAL_DEFAULT="$HOME/phalanx-ci-fixer-testbed"
    ;;
  ts|typescript)
    LANG_ROW="ts"
    TESTBED_REPO_DEFAULT="usephalanx/phalanx-ci-fixer-testbed-ts"
    TESTBED_LOCAL_DEFAULT="$HOME/phalanx-ci-fixer-testbed-ts"
    ;;
  js|javascript)
    LANG_ROW="js"
    TESTBED_REPO_DEFAULT="usephalanx/phalanx-ci-fixer-testbed-js"
    TESTBED_LOCAL_DEFAULT="$HOME/phalanx-ci-fixer-testbed-js"
    ;;
  java)
    TESTBED_REPO_DEFAULT="usephalanx/phalanx-ci-fixer-testbed-java"
    TESTBED_LOCAL_DEFAULT="$HOME/phalanx-ci-fixer-testbed-java"
    ;;
  csharp|cs)
    LANG_ROW="csharp"
    TESTBED_REPO_DEFAULT="usephalanx/phalanx-ci-fixer-testbed-csharp"
    TESTBED_LOCAL_DEFAULT="$HOME/phalanx-ci-fixer-testbed-csharp"
    ;;
  *)
    echo "unknown LANG_ROW: $LANG_ROW (expected python|ts|js|java|csharp)" >&2; exit 2 ;;
esac
TESTBED_REPO="${TESTBED_REPO:-$TESTBED_REPO_DEFAULT}"
TESTBED_LOCAL="${TESTBED_LOCAL:-$TESTBED_LOCAL_DEFAULT}"
FIXTURE_DIR="${FIXTURE_DIR:-$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/scorecard/$LANG_ROW}"
FLAKE_MAX_RETRIGGERS="${FLAKE_MAX_RETRIGGERS:-6}"
INTRO_CI_WAIT_SECS="${INTRO_CI_WAIT_SECS:-360}"

RUN_ID="$(date -u +%Y%m%d-%H%M%S)"

CELL="${1:-}"
[ -z "$CELL" ] && { echo "usage: $0 <lint|test_fail|flake|coverage|all>"; exit 2; }

mkdir -p "$FIXTURE_DIR"

# ── Cell config ──────────────────────────────────────────────────────────
# name | patch | failing_command | failing_job_name | can_flake
CELL_CONFIG() {
  if [ "$LANG_ROW" = "ts" ] || [ "$LANG_ROW" = "js" ]; then
    # TS and JS testbeds share the same CI workflow shape:
    # npm-based toolchain, Lint (eslint+prettier) + Test+Coverage (jest) jobs.
    # Cell-to-patch mapping is identical; only the patch CONTENT differs
    # between the two testbed repos (one patches .ts, the other .js).
    case "$1" in
      lint)      echo "01-lint.patch|npm run lint|Lint|0" ;;
      test_fail) echo "02-test-assertion.patch|npm test|Test + Coverage|0" ;;
      flake)     echo "03-flake-sleep.patch|npm test|Test + Coverage|1" ;;
      coverage)  echo "04-coverage-drop.patch|npm test|Test + Coverage|0" ;;
      *) echo ""; return 1 ;;
    esac
  elif [ "$LANG_ROW" = "java" ]; then
    # Java testbed: Maven + Checkstyle + JaCoCo. Lint = checkstyle:check,
    # Test + Coverage = verify (runs surefire + JaCoCo 80% gate).
    case "$1" in
      lint)      echo "01-lint.patch|mvn -B checkstyle:check|Lint|0" ;;
      test_fail) echo "02-test-assertion.patch|mvn -B verify|Test + Coverage|0" ;;
      flake)     echo "03-flake-sleep.patch|mvn -B verify|Test + Coverage|1" ;;
      coverage)  echo "04-coverage-drop.patch|mvn -B verify|Test + Coverage|0" ;;
      *) echo ""; return 1 ;;
    esac
  elif [ "$LANG_ROW" = "csharp" ]; then
    # C# testbed: .NET 8 SDK + xUnit + coverlet. Lint = `dotnet format --verify`,
    # Test + Coverage = `dotnet test` with coverlet 80% line threshold.
    case "$1" in
      lint)      echo "01-lint.patch|dotnet format --verify-no-changes|Lint|0" ;;
      test_fail) echo "02-test-assertion.patch|dotnet test --no-restore|Test + Coverage|0" ;;
      flake)     echo "03-flake-sleep.patch|dotnet test --no-restore|Test + Coverage|1" ;;
      coverage)  echo "04-coverage-drop.patch|dotnet test --no-restore|Test + Coverage|0" ;;
      *) echo ""; return 1 ;;
    esac
  else
    case "$1" in
      lint)      echo "01-lint-e501.patch|ruff check .|Lint|0" ;;
      test_fail) echo "02-test-assertion.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|0" ;;
      flake)     echo "03-flake-sleep.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|1" ;;
      coverage)  echo "04-coverage-drop.patch|pytest --cov=src/calc --cov-fail-under=80 --timeout=2|Test + Coverage|0" ;;
      *) echo ""; return 1 ;;
    esac
  fi
}

# ── Helpers ──────────────────────────────────────────────────────────────
c_green() { printf "\033[32m%s\033[0m" "$1"; }
c_red()   { printf "\033[31m%s\033[0m" "$1"; }
c_dim()   { printf "\033[2m%s\033[0m"  "$1"; }
say()     { printf "%s\n" "$*"; }

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
if not latest:
    sys.exit(1)
for c in latest.values():
    if not c.get("conclusion"):
        sys.exit(1)
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

# ── Per-cell recorder ────────────────────────────────────────────────────
record_cell() {
  local cell="$1"
  local cfg; cfg=$(CELL_CONFIG "$cell") || { say "$(c_red "unknown cell"): $cell"; return 1; }
  IFS='|' read -r patch cmd job_name can_flake <<< "$cfg"

  local branch="record/${cell}-${RUN_ID}"
  local fixture_path="${FIXTURE_DIR}/${cell}.json"
  local container_fixture="/tmp/fixture-${cell}-${RUN_ID}.json"

  say ""
  say "━━━ recording cell: $(c_green "$cell") ━━━ branch: $branch"

  # 1. Create failure branch
  (
    cd "$TESTBED_LOCAL"
    git fetch origin --quiet
    git checkout main --quiet
    git pull --ff-only --quiet
    git checkout -B "$branch" main --quiet
    if git apply --check "failures/$patch" 2>/dev/null; then
      git apply "failures/$patch"
    else
      git apply --recount "failures/$patch" 2>&1 | head -3 || {
        # Fallback: patch 04 has a known trailing-newline quirk; apply manually
        if [ "$patch" = "04-coverage-drop.patch" ]; then
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
      }
    fi
    git add -A
    git -c user.name="record-bot" -c user.email="bot@phalanx.local" \
        commit -m "record/$cell: intro failure" --quiet
    git push -u origin "$branch" --quiet
  )

  local intro_sha
  intro_sha=$(cd "$TESTBED_LOCAL" && git rev-parse "$branch")
  say "$(c_dim "  [setup]") intro commit=$intro_sha"

  # 2. Open PR so CI fires
  local pr_resp pr_num
  pr_resp=$(curl -s -X POST \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$TESTBED_REPO/pulls" \
    -d "{\"title\":\"record/${cell} [$RUN_ID]\",\"head\":\"$branch\",\"base\":\"main\"}")
  pr_num=$(echo "$pr_resp" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("number") or "")' 2>/dev/null)
  say "$(c_dim "  [setup]") opened PR #$pr_num"

  # 3. Wait for CI to fail (retrigger for flake)
  local ci_line job_id=""
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
      say "$(c_red "  [ci]") $job_name did not fail"
      return 1
    fi
  done
  say "$(c_dim "  [ci]") failing job $job_name id=$job_id"

  # 4. simulate --record on prod
  say "$(c_dim "  [record]") running simulate --record (may take 3-10 min)…"
  ssh_prod "docker exec $CONTAINER python -m phalanx.ci_fixer_v2.simulate \
    --repo $TESTBED_REPO --pr ${pr_num:-0} \
    --branch $branch --sha $intro_sha \
    --job-id $job_id \
    --failing-command $(printf %q "$cmd") \
    --failing-job-name $(printf %q "$job_name") \
    --record $container_fixture \
    --cell-name ${LANG_ROW}_${cell}" 2>&1 | tail -20

  # 5. docker cp fixture out, scp to Mac
  say "$(c_dim "  [copy]") pulling fixture from container → local"
  ssh_prod "docker cp $CONTAINER:$container_fixture /tmp/$(basename $container_fixture)"
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
    "$PROD_HOST:/tmp/$(basename $container_fixture)" "$fixture_path" >/dev/null
  say "$(c_green "  [wrote]") $fixture_path ($(wc -c < "$fixture_path" | tr -d ' ') bytes)"

  # 6. Cleanup
  if [ -n "$pr_num" ]; then
    curl -s -X PATCH -H "Authorization: Bearer $GH_TOKEN" \
      "https://api.github.com/repos/$TESTBED_REPO/pulls/$pr_num" \
      -d '{"state":"closed"}' > /dev/null || true
  fi
  curl -s -X DELETE -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$TESTBED_REPO/git/refs/heads/$branch" > /dev/null || true
  say "$(c_dim "  [cleanup]") PR closed + branch deleted"
}

# ── Main ────────────────────────────────────────────────────────────────
GH_TOKEN="$(gh_token)"
[ -z "$GH_TOKEN" ] && { say "$(c_red "fatal"): no github token"; exit 2; }
export GH_TOKEN

if [ "$CELL" = "all" ]; then
  for c in lint test_fail flake coverage; do
    record_cell "$c" || say "$(c_red "FAILED"): $c (continuing)"
  done
else
  record_cell "$CELL"
fi

say ""
say "━━━ fixtures in $FIXTURE_DIR ━━━"
ls -la "$FIXTURE_DIR" 2>/dev/null | grep -v '^total' || say "(none)"
