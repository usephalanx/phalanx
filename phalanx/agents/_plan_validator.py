"""v1.7 plan validator — deterministic structural checks on TL's task_plan.

Commander calls `validate_plan(plan)` after TL emits its task_plan
output. If the plan is malformed (unknown agent, cycle, missing
dependency, etc.), commander marks the TL task FAILED and re-dispatches
TL with feedback in the next attempt.

This validator does NO semantic checks (e.g., "is this fix correct?").
It only asserts the plan is well-formed enough for commander to safely
persist + dispatch. Semantic correctness is TL's job.

See docs/v17-tl-as-planner.md §7.2 for the canonical reference.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from phalanx.agents._v17_types import (
    V17_AGENT_REGISTRY,
    PlanValidationError,
    TaskSpec,
)


# v1.7.2.5 — apply_diff hunk header validation.
#
# `git apply` requires unified-diff hunk headers in the form
#   @@ -<start>[,<count>] +<start>[,<count>] @@ [optional context]
#
# GPT-5.4 has been observed under repetition (2026-05-04 soak) emitting
# "fuzzy" hunks: bare `@@` markers with no line numbers. Those are valid
# in informal/human-readable patches but `git apply` rejects them with
#   error: No valid patches in input (allow with "--allow-empty")
#
# The plan validator catches these BEFORE engineer dispatch so commander
# rejects the TL output and forces a re-plan instead of letting the
# engineer fail mid-step (which costs an iteration and clouds the soak
# signal).
#
# A valid hunk header MUST:
#   1. Start with `@@ -`
#   2. Have at least <start> after the minus
#   3. Have a `+` section after the from-range
#   4. Close with ` @@` (or `@@\n` with optional trailing context)
#
# Pattern accepts:   @@ -12,5 +12,7 @@
#                    @@ -12 +12 @@
#                    @@ -12,5 +12,7 @@ def foo():
#                    @@ -0,0 +1,42 @@        (new file)
# Pattern rejects:   @@
#                    @@\n
#                    @@ -, +, @@
_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@.*$",
    re.MULTILINE,
)

# A line that starts with `@@` but isn't a valid hunk header.
_FUZZY_HUNK_RE = re.compile(r"^@@\s*$", re.MULTILINE)

# A diff body MUST contain at least one valid hunk header (or be a pure
# new-file/delete-file mode, which we don't currently support — TL would
# need to send a `replace` for empty-file creation).
_DIFF_HAS_FILE_HEADERS_RE = re.compile(
    r"^---\s+\S.*\n\+\+\+\s+\S",
    re.MULTILINE,
)

# Per-action required fields. We don't check semantic validity (e.g.,
# whether `old` is in the file) — that's c5 self-critique's job at TL
# emit time. Here we only check structural completeness.
_STEP_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "read": frozenset({"file"}),
    "replace": frozenset({"file", "old", "new"}),
    "insert": frozenset({"file", "after_line", "content"}),
    "delete_lines": frozenset({"file", "line"}),
    "apply_diff": frozenset({"diff"}),
    "run": frozenset({"command"}),
    "commit": frozenset({"message"}),
    "push": frozenset(),
}


def validate_plan(
    plan: list[TaskSpec],
    *,
    completed_task_ids: set[str] | None = None,
) -> None:
    """Raise PlanValidationError if plan is malformed.

    Rules:
      1. Plan must be a non-empty list of dicts
      2. Each task_spec has unique task_id, valid agent, valid steps shape
      3. depends_on references known task_ids (in plan OR completed_task_ids)
      4. No cycles
      5. Plan terminates in cifix_sre_verify (only the LAST task in
         topological order may be cifix_sre_verify; nothing depends on
         a verify in a way that would defeat its purpose)
      6. Per-step required fields present for each action

    `completed_task_ids` lets REPLAN mode reference already-finished tasks.
    """
    if not isinstance(plan, list) or not plan:
        raise PlanValidationError("plan must be a non-empty list")

    completed = set(completed_task_ids or [])
    seen_ids: set[str] = set()

    # Pass 1: per-task structural validation
    for i, ts in enumerate(plan):
        if not isinstance(ts, dict):
            raise PlanValidationError(f"plan[{i}] is not a dict")

        task_id = ts.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PlanValidationError(f"plan[{i}].task_id missing or empty")
        if task_id in seen_ids:
            raise PlanValidationError(f"duplicate task_id: {task_id!r}")
        if task_id in completed:
            raise PlanValidationError(
                f"task_id {task_id!r} collides with completed task_id"
            )
        seen_ids.add(task_id)

        agent = ts.get("agent")
        if agent not in V17_AGENT_REGISTRY:
            raise PlanValidationError(
                f"task {task_id!r}: unknown agent {agent!r}; "
                f"must be one of {sorted(V17_AGENT_REGISTRY)}"
            )

        depends_on = ts.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise PlanValidationError(
                f"task {task_id!r}: depends_on must be a list, got {type(depends_on).__name__}"
            )

        # Agent-specific shape checks
        if agent == "cifix_sre_setup":
            _validate_sre_setup_shape(ts)
        else:
            _validate_executor_shape(ts)

    # Pass 2: dependency resolution
    plan_ids = {ts["task_id"] for ts in plan}
    for ts in plan:
        for dep in ts.get("depends_on") or []:
            if dep not in plan_ids and dep not in completed:
                raise PlanValidationError(
                    f"task {ts['task_id']!r} depends on unknown {dep!r} "
                    f"(neither in this plan nor in completed tasks)"
                )

    # Pass 3: cycle detection (topological sort over plan-internal deps)
    sorted_ids = _topological_sort_or_raise(plan)

    # Pass 4: terminal task must be sre_verify
    last_task = next(ts for ts in plan if ts["task_id"] == sorted_ids[-1])
    if last_task["agent"] != "cifix_sre_verify":
        raise PlanValidationError(
            f"plan must terminate in cifix_sre_verify; "
            f"got {last_task['agent']!r} at task {last_task['task_id']!r}"
        )


def _validate_sre_setup_shape(ts: TaskSpec) -> None:
    """SRE setup tasks need env_requirements; steps optional."""
    env = ts.get("env_requirements")
    if not isinstance(env, dict):
        raise PlanValidationError(
            f"task {ts['task_id']!r}: cifix_sre_setup requires env_requirements dict"
        )
    if "reproduce_command" not in env or not isinstance(env["reproduce_command"], str):
        raise PlanValidationError(
            f"task {ts['task_id']!r}: env_requirements.reproduce_command "
            f"required (string)"
        )


def _validate_executor_shape(ts: TaskSpec) -> None:
    """Engineer + sre_verify tasks: steps required + per-action checks."""
    steps = ts.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanValidationError(
            f"task {ts['task_id']!r}: agent {ts['agent']!r} requires non-empty steps list"
        )
    for j, step in enumerate(steps):
        if not isinstance(step, dict):
            raise PlanValidationError(
                f"task {ts['task_id']!r}.steps[{j}] is not a dict"
            )
        action = step.get("action")
        if action not in _STEP_REQUIRED_FIELDS:
            raise PlanValidationError(
                f"task {ts['task_id']!r}.steps[{j}]: unknown action {action!r}; "
                f"must be one of {sorted(_STEP_REQUIRED_FIELDS)}"
            )
        required = _STEP_REQUIRED_FIELDS[action]
        for field in required:
            if step.get(field) in (None, ""):
                raise PlanValidationError(
                    f"task {ts['task_id']!r}.steps[{j}] (action={action!r}): "
                    f"missing required field {field!r}"
                )

        # v1.7.2.5 — apply_diff specifically: the diff text must be valid
        # `git apply` input. Fuzzy hunks (bare `@@`) are rejected here so
        # commander forces TL to re-plan with `replace`/`insert` (or a
        # properly-formed unified diff) before engineer ever runs.
        if action == "apply_diff":
            _validate_apply_diff_step(ts["task_id"], j, step.get("diff") or "")


def _validate_apply_diff_step(task_id: str, step_idx: int, diff_text: str) -> None:
    """Reject apply_diff steps whose diff body isn't valid `git apply` input.

    Forces TL to either:
      - emit a unified diff with proper hunk headers
        (`@@ -<start>[,<count>] +<start>[,<count>] @@`)
      - switch to `replace`/`insert` for small targeted edits

    Failure modes from the 2026-05-04 soak:
      - `@@\\n` placeholders (no line numbers)
      - missing --- / +++ file headers
      - completely empty diff body
    """
    text = diff_text or ""
    where = f"task {task_id!r}.steps[{step_idx}] (action='apply_diff')"

    # Empty / whitespace-only
    if not text.strip():
        raise PlanValidationError(
            f"{where}: diff body is empty. "
            f"Either emit a proper unified diff or use replace/insert."
        )

    # Must contain --- / +++ file headers somewhere (the file-pair the
    # diff applies to). Without these, `git apply` can't bind the diff
    # to a path.
    if not _DIFF_HAS_FILE_HEADERS_RE.search(text):
        raise PlanValidationError(
            f"{where}: diff missing `--- a/<path>` and `+++ b/<path>` "
            f"file headers. `git apply` requires file headers."
        )

    # Reject fuzzy `@@` hunk markers (no line numbers).
    fuzzy_matches = _FUZZY_HUNK_RE.findall(text)
    if fuzzy_matches:
        raise PlanValidationError(
            f"{where}: diff contains {len(fuzzy_matches)} fuzzy hunk header(s) "
            f"(`@@` with no line numbers). `git apply` rejects these. "
            f"Use `@@ -<start>[,<count>] +<start>[,<count>] @@` form, "
            f"OR switch to replace/insert steps for targeted edits."
        )

    # Must contain at least one well-formed hunk header. (Reject diffs
    # that have file headers but no hunks at all — useless to git apply.)
    if not _HUNK_HEADER_RE.search(text):
        raise PlanValidationError(
            f"{where}: diff has no valid hunk headers. "
            f"Each hunk MUST start with "
            f"`@@ -<start>[,<count>] +<start>[,<count>] @@`."
        )


def _topological_sort_or_raise(plan: list[TaskSpec]) -> list[str]:
    """Kahn's algorithm. Returns ids in topological order or raises on cycle.

    Only considers plan-internal dependencies (depends_on entries that
    reference completed tasks are treated as already-satisfied edges).
    """
    plan_ids = {ts["task_id"] for ts in plan}
    in_degree: dict[str, int] = {ts["task_id"]: 0 for ts in plan}
    forward: dict[str, list[str]] = defaultdict(list)

    for ts in plan:
        for dep in ts.get("depends_on") or []:
            if dep in plan_ids:
                forward[dep].append(ts["task_id"])
                in_degree[ts["task_id"]] += 1

    # Start with all zero-in-degree nodes; process in insertion order so
    # the "last task" rule is deterministic when multiple terminals exist.
    queue: list[str] = [tid for tid in [ts["task_id"] for ts in plan] if in_degree[tid] == 0]
    order: list[str] = []

    while queue:
        node = queue.pop(0)
        order.append(node)
        for child in forward[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(plan):
        unresolved = [tid for tid, deg in in_degree.items() if deg > 0]
        raise PlanValidationError(
            f"plan contains cycle; unresolved nodes: {sorted(unresolved)}"
        )

    return order


def is_plan(value: Any) -> bool:
    """Quick shape check used by commander before invoking the full validator."""
    return isinstance(value, list) and bool(value) and all(
        isinstance(x, dict) for x in value
    )
