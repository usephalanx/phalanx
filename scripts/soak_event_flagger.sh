#!/usr/bin/env bash
# v1.7.2.7 soak event flagger — given a JSONL row on stdin, emit one
# line per soak run with hard flags for the 5 anti-patterns:
#
#   F1 — coverage cell + apply_diff for small edits
#   F2 — flake cell + test deletion / skip / assertion-reduction
#   F3 — iter-2 same-strategy replan repeat
#   F4 — gate verdict NOT_FIXED or REGRESSION
#   F5 — run exited without terminal state
#
# Reads ONE JSONL row from stdin. Queries prod via SSH for per-task
# error data. Emits one heartbeat line + zero-or-more 🚩 flag lines.

set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/work/aws/LightsailDefaultKey-us-west-2.pem}"
SSH_HOST="${SSH_HOST:-ubuntu@44.233.157.41}"
PG_CONTAINER="${PG_CONTAINER:-phalanx-prod-postgres-1}"

# Parse the row via python (handles control chars cleanly)
parsed=$(python3 -c '
import json, sys
try:
    raw = sys.stdin.buffer.read()
    d = json.loads(raw)
except Exception as e:
    print("PARSE_ERROR\t" + str(e), file=sys.stderr)
    sys.exit(0)
fields = [
    d.get("run_id") or "",
    d.get("cell") or "?",
    str(d.get("verdict")) if d.get("verdict") is not None else "null",
    str(d.get("iterations")) if d.get("iterations") is not None else "?",
    str(d.get("gate_decision")) if d.get("gate_decision") is not None else "null",
    d.get("ts_start") or "",
]
print("\t".join(fields))
')

[ -z "$parsed" ] && exit 0
IFS=$'\t' read -r run_id cell verdict iters gate ts <<< "$parsed"
ts_short=${ts:5:14}
short_run="${run_id:0:8}"

flags=("")  # initialize for `set -u` safety; first empty entry filtered below

# F5 — null verdict (runner couldn't determine outcome even after fallback)
[ "$verdict" = "null" ] && flags+=("🚩 F5 NULL-VERDICT")

# F4 — gate verdict NOT_FIXED or REGRESSION
case "$gate" in
  NOT_FIXED|REGRESSION) flags+=("🚩 F4 GATE-$gate") ;;
esac

# Deeper queries require a run_id
if [ -n "$run_id" ] && [ "$run_id" != "null" ]; then
  task_errs=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
    "$SSH_HOST" \
    "docker exec $PG_CONTAINER psql -tA -F'|' -U forge -d forge -c \"
SELECT agent_role || '|' ||
       COALESCE(output->>'error_class','') || '|' ||
       COALESCE(LEFT(output->>'validation_error',300),'') || '|' ||
       COALESCE(LEFT(error,300),'')
FROM tasks WHERE run_id='$run_id' AND status IN ('FAILED','COMPLETED');
\"" 2>/dev/null) || task_errs=""

  while IFS='|' read -r role ec verr terr; do
    [ -z "$role" ] && continue

    # F1 — coverage + apply_diff threshold/fuzzy at validator
    if [ "$cell" = "coverage" ] && [ "$ec" = "plan_validation_failed" ]; then
      [[ "$verr" == *"below the > 5 threshold"* ]] && flags+=("🚩 F1 APPLY_DIFF-SMALL-EDITS-COVERAGE")
      [[ "$verr" == *"fuzzy hunk header"* ]] && flags+=("🚩 F1 APPLY_DIFF-FUZZY-COVERAGE")
    fi

    # F2 — flake-cell guardrails firing (bad strategy attempt blocked)
    if [ "$cell" = "flake" ]; then
      if [[ "$ec" == "self_critique_inconsistent"* ]] && \
         [[ "$verr$terr" == *"test_behavior_preserved"* ]]; then
        flags+=("🚩 F2 FLAKE-C8-FIRED")
      fi
      [[ "$terr" == *"patch_safety_violation:test_deletion"* ]] && \
        flags+=("🚩 F2 FLAKE-PATCH-SAFETY-test_deletion")
      [[ "$terr" == *"patch_safety_violation:assertion_reduction"* ]] && \
        flags+=("🚩 F2 FLAKE-PATCH-SAFETY-assertion_reduction")
      [[ "$terr" == *"patch_safety_violation:test_function_reduction"* ]] && \
        flags+=("🚩 F2 FLAKE-PATCH-SAFETY-test_function_reduction")
      [[ "$terr" == *"patch_safety_violation:skip_injection"* ]] && \
        flags+=("🚩 F2 FLAKE-PATCH-SAFETY-skip_injection")
    fi

    # F3 — same-strategy replan
    [[ "$verr" == *"identical strategy signature"* ]] && \
      flags+=("🚩 F3 SAME-STRATEGY-REPLAN")
  done <<< "$task_errs"
fi

status_emoji="✅"
[ "$verdict" = "FAILED" ] && status_emoji="❌"
[ "$verdict" = "null" ] && status_emoji="❓"

# Always emit a heartbeat
cell_pad="$(printf '%-10s' "$cell")"
echo "$ts_short  cell=$cell_pad  $status_emoji $verdict iters=$iters gate=$gate run=$short_run"
for f in "${flags[@]}"; do
  [ -z "$f" ] && continue
  echo "    $f"
done
