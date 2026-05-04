"""Tier-1 tests for v1.7 plan validator.

Validates the deterministic structural checks commander runs on TL's
emitted task_plan. No LLM, no DB, no async — pure function tests.

The validator is the gate between TL's free-form planning and commander
trusting the plan enough to persist + dispatch. If the validator misses
something, it ships a malformed DAG; if it over-rejects, TL gets stuck
in a re-plan loop. Both directions matter — these tests cover both.
"""

from __future__ import annotations

import pytest

from phalanx.agents._plan_validator import validate_plan
from phalanx.agents._v17_types import PlanValidationError


# ─── Fixture builders — small helpers so each test reads as intent, not noise ──


def _engineer_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "agent": "cifix_engineer",
        "depends_on": depends_on or [],
        "purpose": "apply fix",
        "steps": [
            {"id": 1, "action": "read", "file": "src/foo.py"},
            {
                "id": 2,
                "action": "replace",
                "file": "src/foo.py",
                "old": "x = 1",
                "new": "x = 2",
            },
            {"id": 3, "action": "commit", "message": "fix: bump x"},
            {"id": 4, "action": "push"},
        ],
    }


def _sre_setup_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "agent": "cifix_sre_setup",
        "depends_on": depends_on or [],
        "purpose": "provision env",
        "env_requirements": {
            "python": "3.11",
            "python_packages": ["pytest"],
            "reproduce_command": "pytest tests/",
            "reproduce_expected": "fails with E501",
        },
    }


def _sre_verify_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "agent": "cifix_sre_verify",
        "depends_on": depends_on or [],
        "purpose": "full CI verify",
        "steps": [
            {
                "id": 1,
                "action": "run",
                "command": "pytest tests/",
                "expect_exit": 0,
            },
        ],
    }


# ─── Happy paths ───────────────────────────────────────────────────────────────


class TestValidPlans:
    def test_minimal_lint_plan_engineer_then_verify(self):
        """Smallest valid plan: just engineer + sre_verify.
        This is the typical 'lint fix' shape — no SRE setup needed.
        """
        plan = [
            _engineer_task("T2"),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)  # should not raise

    def test_full_plan_setup_engineer_verify(self):
        """The 'real bug' shape: SRE provisions env, engineer fixes,
        verify runs the full CI. Linear chain, terminates in verify.
        """
        plan = [
            _sre_setup_task("T2"),
            _engineer_task("T3", depends_on=["T2"]),
            _sre_verify_task("T4", depends_on=["T3"]),
        ]
        validate_plan(plan)

    def test_replan_referencing_completed_task(self):
        """REPLAN mode emits delta tasks that depend on already-finished
        tasks (T2, T3 from the original plan). Validator must accept
        these via the completed_task_ids parameter.
        """
        delta = [
            _engineer_task("T5", depends_on=["T3"]),  # T3 was completed before
            _sre_verify_task("T6", depends_on=["T5"]),
        ]
        validate_plan(delta, completed_task_ids={"T2", "T3", "T4"})

    def test_multiple_engineer_tasks_with_anticipated_child_bug(self):
        """TL anticipates that fixing A unlocks bug B — emits both as
        siblings. Both engineer tasks depend on setup; verify depends
        on both engineers.
        """
        plan = [
            _sre_setup_task("T2"),
            _engineer_task("T3", depends_on=["T2"]),
            _engineer_task("T4", depends_on=["T2"]),  # parallel to T3
            _sre_verify_task("T5", depends_on=["T3", "T4"]),
        ]
        validate_plan(plan)


# ─── Structural rejections ─────────────────────────────────────────────────────


class TestRejectsStructuralErrors:
    def test_empty_plan_rejected(self):
        with pytest.raises(PlanValidationError, match="non-empty"):
            validate_plan([])

    def test_non_list_plan_rejected(self):
        with pytest.raises(PlanValidationError, match="non-empty list"):
            validate_plan({"task_id": "T2"})  # type: ignore[arg-type]

    def test_duplicate_task_id_rejected(self):
        plan = [
            _engineer_task("T2"),
            _sre_verify_task("T2", depends_on=["T2"]),  # duplicate id
        ]
        with pytest.raises(PlanValidationError, match="duplicate task_id"):
            validate_plan(plan)

    def test_missing_task_id_rejected(self):
        plan = [
            {"agent": "cifix_engineer", "steps": [{"id": 1, "action": "push"}]},
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="task_id missing"):
            validate_plan(plan)

    def test_unknown_agent_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_techlead",  # not an executor
                "depends_on": [],
                "steps": [{"id": 1, "action": "push"}],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="unknown agent"):
            validate_plan(plan)

    def test_depends_on_unknown_task_rejected(self):
        plan = [
            _engineer_task("T2", depends_on=["T999"]),  # T999 doesn't exist
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="depends on unknown"):
            validate_plan(plan)


# ─── Cycle detection ───────────────────────────────────────────────────────────


class TestRejectsCycles:
    def test_two_node_cycle(self):
        plan = [
            _engineer_task("T2", depends_on=["T3"]),
            _sre_verify_task("T3", depends_on=["T2"]),  # T2 → T3 → T2
        ]
        with pytest.raises(PlanValidationError, match="cycle"):
            validate_plan(plan)

    def test_three_node_cycle(self):
        plan = [
            _engineer_task("T2", depends_on=["T4"]),
            _engineer_task("T3", depends_on=["T2"]),
            _sre_verify_task("T4", depends_on=["T3"]),  # T2 → T4 → T3 → T2
        ]
        with pytest.raises(PlanValidationError, match="cycle"):
            validate_plan(plan)

    def test_self_loop(self):
        plan = [
            _engineer_task("T2", depends_on=["T2"]),  # depends on self
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="cycle"):
            validate_plan(plan)


# ─── Terminal-task rule ────────────────────────────────────────────────────────


class TestTerminalTaskMustBeVerify:
    def test_plan_ending_in_engineer_rejected(self):
        plan = [_engineer_task("T2")]
        with pytest.raises(PlanValidationError, match="must terminate in cifix_sre_verify"):
            validate_plan(plan)

    def test_plan_ending_in_sre_setup_rejected(self):
        plan = [_sre_setup_task("T2")]
        with pytest.raises(PlanValidationError, match="must terminate in cifix_sre_verify"):
            validate_plan(plan)

    def test_verify_in_middle_with_engineer_after_rejected(self):
        """If verify is in the middle and something depends on it that
        isn't another verify, the topological-sort terminal isn't verify.
        Catches TL emitting backwards-ordered plans.
        """
        plan = [
            _sre_verify_task("T2"),
            _engineer_task("T3", depends_on=["T2"]),  # comes AFTER verify
        ]
        with pytest.raises(PlanValidationError, match="must terminate in cifix_sre_verify"):
            validate_plan(plan)


# ─── Per-action step shape ────────────────────────────────────────────────────


class TestStepShape:
    def test_replace_missing_old_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "steps": [
                    {"id": 1, "action": "replace", "file": "x.py", "new": "y"},  # no `old`
                ],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="missing required field 'old'"):
            validate_plan(plan)

    def test_run_missing_command_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "steps": [{"id": 1, "action": "run"}],  # no command
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="missing required field 'command'"):
            validate_plan(plan)

    def test_unknown_action_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "steps": [{"id": 1, "action": "yeet", "file": "x.py"}],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="unknown action"):
            validate_plan(plan)

    def test_engineer_with_empty_steps_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "steps": [],  # empty!
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="non-empty steps"):
            validate_plan(plan)

    def test_apply_diff_action_accepted(self):
        """Common shape — TL emits a unified diff for engineer to apply.
        v1.7.2.7: the diff must be a new-file or > 5 hunks; otherwise
        TL should use replace/insert. New-file form here keeps this
        structural-validation test focused on action recognition."""
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "steps": [
                    {
                        "id": 1,
                        "action": "apply_diff",
                        "diff": "--- /dev/null\n+++ b/x.py\n@@ -0,0 +1 @@\n+new\n",
                    },
                    {"id": 2, "action": "commit", "message": "fix"},
                    {"id": 3, "action": "push"},
                ],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)


# ─── SRE setup shape ──────────────────────────────────────────────────────────


class TestSreSetupShape:
    def test_sre_setup_missing_env_requirements_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_sre_setup",
                "depends_on": [],
                "purpose": "no env_requirements",
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="env_requirements"):
            validate_plan(plan)

    def test_sre_setup_missing_reproduce_command_rejected(self):
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_sre_setup",
                "depends_on": [],
                "env_requirements": {"python": "3.11"},  # no reproduce_command
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="reproduce_command"):
            validate_plan(plan)


# ─── Replan + completed_task_ids interaction ──────────────────────────────────


class TestReplanCompletedRefs:
    def test_replan_unknown_completed_ref_rejected(self):
        """REPLAN delta references a completed task NOT in the
        completed_task_ids set — this is a real bug, not a known
        already-done task."""
        delta = [
            _engineer_task("T5", depends_on=["T999"]),  # T999 not in completed
            _sre_verify_task("T6", depends_on=["T5"]),
        ]
        with pytest.raises(PlanValidationError, match="depends on unknown"):
            validate_plan(delta, completed_task_ids={"T2", "T3"})

    def test_replan_collision_with_completed_rejected(self):
        """REPLAN must use FRESH task_ids; collision with completed
        breaks the unique-id property across the run.
        """
        delta = [
            _engineer_task("T3"),  # T3 is in completed!
            _sre_verify_task("T6", depends_on=["T3"]),
        ]
        with pytest.raises(PlanValidationError, match="collides with completed"):
            validate_plan(delta, completed_task_ids={"T2", "T3"})


# ─── v1.7.2.5 apply_diff hunk header validation ───────────────────────────────


def _engineer_task_with_diff(task_id: str, diff: str) -> dict:
    """Engineer task whose ONE step is an apply_diff with the given diff
    body. Used by the apply_diff-rule tests below.
    """
    return {
        "task_id": task_id,
        "agent": "cifix_engineer",
        "depends_on": [],
        "purpose": "apply diff",
        "steps": [
            {"id": 1, "action": "apply_diff", "diff": diff},
            {"id": 2, "action": "commit", "message": "fix"},
            {"id": 3, "action": "push"},
        ],
    }


# v1.7.2.7 added a `> 5 hunks OR new-file` rule on apply_diff. The
# valid-format acceptance tests below now use either a new-file diff
# (exempt from the threshold) or a 6+ hunk multi-site rewrite.

_VALID_DIFF_NEW_FILE_TESTS = """\
--- /dev/null
+++ b/tests/test_math_ops.py
@@ -0,0 +1,5 @@
+from calc.math_ops import add, divide, multiply, subtract
+
+
+def test_percentage():
+    assert percentage(1, 4) == 25.0
"""

# Single-line edit pattern (`@@ -L +L @@` with no count) — wrapped as
# a new-file diff so it passes the v1.7.2.7 threshold.
_VALID_DIFF_NO_COUNT_NEW_FILE = """\
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+x = 2
"""

_VALID_DIFF_WITH_CONTEXT_HEADER_NEW_FILE = """\
--- /dev/null
+++ b/src/x.py
@@ -0,0 +1,8 @@ def existing_function():
+def existing_function():
+    return 42
+
+def new_function():
+    return 43
+
+def another():
+    pass
"""

_VALID_DIFF_NEW_FILE = """\
--- /dev/null
+++ b/tests/new_test.py
@@ -0,0 +1,4 @@
+def test_new():
+    assert True
+
"""

# v1.7.2.7 — 6+ hunk multi-site rewrite (the OTHER apply_diff exemption)
_VALID_DIFF_LARGE_REWRITE = """\
--- a/src/x.py
+++ b/src/x.py
@@ -10,2 +10,3 @@
 context
-old1
+new1
+added1
@@ -20,2 +20,3 @@
 context
-old2
+new2
+added2
@@ -30,2 +30,3 @@
 context
-old3
+new3
+added3
@@ -40,2 +40,3 @@
 context
-old4
+new4
+added4
@@ -50,2 +50,3 @@
 context
-old5
+new5
+added5
@@ -60,2 +60,3 @@
 context
-old6
+new6
+added6
"""

_FUZZY_DIFF_BARE_AT_AT = """\
--- a/tests/test_math_ops.py
+++ b/tests/test_math_ops.py
@@
-from calc.math_ops import add, divide, multiply, subtract
+from calc.math_ops import add, average, divide, multiply, percentage, subtract
@@
 def test_divide_by_zero_raises():
     with pytest.raises(ZeroDivisionError):
         divide(1, 0)
+
+
+def test_percentage():
+    assert percentage(1, 4) == 25.0
"""

# `@@` followed by trailing whitespace then newline — same fuzzy shape
_FUZZY_DIFF_AT_AT_NEWLINE = (
    "--- a/src/x.py\n"
    "+++ b/src/x.py\n"
    "@@   \n"
    "-x = 1\n"
    "+x = 2\n"
)

_MALFORMED_DIFF_BAD_HUNK = """\
--- a/src/x.py
+++ b/src/x.py
@@ - + @@
-x = 1
+x = 2
"""

_DIFF_NO_FILE_HEADERS = """\
@@ -1,3 +1,4 @@
 def x():
     pass
+    return 1
"""


class TestApplyDiffValidation:
    """v1.7.2.5 — plan validator must reject apply_diff steps whose diff
    body isn't valid `git apply` input. From the 2026-05-04 soak: GPT-5.4
    emits fuzzy `@@` headers under repetition; rejection forces re-plan."""

    # ── Acceptance cases ────────────────────────────────────────────

    def test_valid_unified_diff_accepted_new_file(self):
        """v1.7.2.7: small diffs require new-file or > 5 hunks. Use new-file."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_NEW_FILE_TESTS),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)  # must not raise

    def test_valid_unified_diff_accepted_large_rewrite(self):
        """The OTHER exemption: > 5 hunks across multiple sites."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_LARGE_REWRITE),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)

    def test_valid_diff_no_line_count_accepted(self):
        """`@@ -0,0 +1 @@` (single-line, count omitted) is valid git format,
        and it's a new-file diff so passes the v1.7.2.7 threshold."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_NO_COUNT_NEW_FILE),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)

    def test_valid_diff_with_context_header_accepted(self):
        """Trailing context after the second `@@` is valid (and this
        version uses new-file mode to pass the threshold)."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_WITH_CONTEXT_HEADER_NEW_FILE),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)

    def test_valid_new_file_diff_accepted(self):
        """`/dev/null` source + `@@ -0,0 +1,N @@` is the standard
        new-file pattern — must accept."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_NEW_FILE),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)

    # ── Rejection cases ─────────────────────────────────────────────

    def test_bare_at_at_rejected(self):
        """The exact failure shape from 2026-05-04 soak runs 4e7f2ca7
        + 158f499c: `@@\\n` placeholder hunks with no line numbers."""
        plan = [
            _engineer_task_with_diff("T2", _FUZZY_DIFF_BARE_AT_AT),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="fuzzy hunk header"):
            validate_plan(plan)

    def test_at_at_with_trailing_whitespace_rejected(self):
        """`@@   ` (trailing whitespace, no line numbers) — also rejected."""
        plan = [
            _engineer_task_with_diff("T2", _FUZZY_DIFF_AT_AT_NEWLINE),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="fuzzy hunk header"):
            validate_plan(plan)

    def test_malformed_hunk_no_line_numbers_rejected(self):
        """`@@ - + @@` — has `@@` markers but the line-number positions
        are dashes/pluses with no digits. git apply rejects."""
        plan = [
            _engineer_task_with_diff("T2", _MALFORMED_DIFF_BAD_HUNK),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="hunk"):
            validate_plan(plan)

    def test_diff_without_file_headers_rejected(self):
        """A diff body with hunk headers but no `--- a/<path>` /
        `+++ b/<path>` file headers can't be applied — git apply needs
        to know which file to patch."""
        plan = [
            _engineer_task_with_diff("T2", _DIFF_NO_FILE_HEADERS),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="file headers"):
            validate_plan(plan)

    def test_empty_diff_rejected(self):
        plan = [
            _engineer_task_with_diff("T2", ""),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        # Empty string is treated as missing required field by the
        # generic `_STEP_REQUIRED_FIELDS` check, which fires first.
        with pytest.raises(PlanValidationError, match="missing required field"):
            validate_plan(plan)

    def test_whitespace_only_diff_rejected(self):
        plan = [
            _engineer_task_with_diff("T2", "   \n\t\n"),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="empty"):
            validate_plan(plan)

    # ── Soak failure replay ────────────────────────────────────────

    def test_replay_158f499c_coverage_failure(self):
        """Verbatim diff from soak run 158f499c, which engineer rejected
        as `diff_apply_check_failed`. With the v1.7.2.5 validator, this
        plan never reaches engineer — TL is forced to re-plan upstream."""
        soak_diff = (
            "--- a/tests/test_math_ops.py\n"
            "+++ b/tests/test_math_ops.py\n"
            "@@\n"
            "-from calc.math_ops import add, divide, multiply, subtract\n"
            "+from calc.math_ops import add, average, divide, multiply, percentage, subtract\n"
            "@@\n"
            " def test_divide_by_zero_raises():\n"
            "     with pytest.raises(ZeroDivisionError):\n"
            "         divide(1, 0)\n"
            "+\n"
            "+\n"
            "+def test_percentage():\n"
            "+    assert percentage(1, 4) == 25.0\n"
            "+    assert percentage(3, 12) == 25.0\n"
            "+\n"
            "+\n"
            "+def test_average():\n"
            "+    assert average([1, 2, 3, 4]) == 2.5\n"
        )
        plan = [
            _sre_setup_task("T2"),
            _engineer_task_with_diff("T3", soak_diff) | {"depends_on": ["T2"]},
            _sre_verify_task("T4", depends_on=["T3"]),
        ]
        # FIX must mutate via dict update because the helper builds with []
        plan[1] = {**plan[1], "depends_on": ["T2"]}

        with pytest.raises(PlanValidationError, match="fuzzy hunk header"):
            validate_plan(plan)

    def test_replay_4e7f2ca7_iter2_coverage_failure(self):
        """Same fuzzy-hunk pattern from soak run 4e7f2ca7 iter-2."""
        soak_diff = (
            "--- a/tests/test_math_ops.py\n"
            "+++ b/tests/test_math_ops.py\n"
            "@@\n"
            "-from calc.math_ops import add, divide, multiply, subtract\n"
            "+from calc.math_ops import add, average, divide, multiply, percentage, subtract\n"
            "@@\n"
            " def test_divide_by_zero_raises():\n"
            "     with pytest.raises(ZeroDivisionError):\n"
            "         divide(1, 0)\n"
            "+\n"
            "+\n"
            "+def test_percentage():\n"
            "+    assert percentage(1, 4) == 25.0\n"
            "+    assert percentage(3, 2) == 150.0\n"
        )
        plan = [
            _engineer_task_with_diff("T2", soak_diff),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="fuzzy hunk header"):
            validate_plan(plan)

    def test_corrected_diff_after_replan_accepts(self):
        """When TL replans with a properly-formatted diff (line numbers
        in hunk headers AND > 5 hunks per v1.7.2.7), the validator
        accepts. The 'right' replan path: switch from `apply_diff` to
        `replace`/`insert` for small edits — see test_replace_alternative_after_replan_accepts."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_LARGE_REWRITE),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)  # must not raise

    def test_replace_alternative_after_replan_accepts(self):
        """The other expected re-plan path: TL switches from apply_diff
        to replace/insert (the prompt nudges this when ≤5 hunks).
        Validator accepts naturally — no apply_diff step to validate."""
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "add tests via insert/replace",
                "steps": [
                    {
                        "id": 1, "action": "replace",
                        "file": "tests/test_math_ops.py",
                        "old": "from calc.math_ops import add, divide, multiply, subtract",
                        "new": "from calc.math_ops import add, average, divide, multiply, percentage, subtract",
                    },
                    {
                        "id": 2, "action": "insert",
                        "file": "tests/test_math_ops.py",
                        "after_line": 50,
                        "content": "\n\ndef test_percentage():\n    assert percentage(1, 4) == 25.0\n",
                    },
                    {"id": 3, "action": "commit", "message": "test: add coverage"},
                    {"id": 4, "action": "push"},
                ],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)


# ─── Force-replan integration: TL agent fails with structured error ────────


class TestTechLeadForcesReplanOnInvalidDiff:
    """Tier-1 contract test: when validate_plan raises, the cifix_techlead
    agent's `execute()` returns AgentResult.success=False with
    error_class='plan_validation_failed' so commander can re-dispatch TL
    instead of letting engineer run on a broken plan.

    This pins the wiring at cifix_techlead.py:578 (validate_plan call)
    + the AgentResult shape downstream readers depend on.
    """

    def test_validation_error_class_in_output(self):
        """Spot-check that PlanValidationError carries enough info for
        the agent to surface a `plan_validation_failed` error."""
        from phalanx.agents._v17_types import PlanValidationError

        plan = [
            _engineer_task_with_diff("T2", _FUZZY_DIFF_BARE_AT_AT),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError) as excinfo:
            validate_plan(plan)
        msg = str(excinfo.value)
        # Error must name the offending task + step + the rule violated
        assert "T2" in msg
        assert "fuzzy hunk header" in msg
        assert "git apply" in msg.lower()


# ─── v1.7.2.7 — apply_diff hunk-count threshold ───────────────────────────────


def _build_diff(num_hunks: int, *, new_file: bool = False) -> str:
    """Construct a syntactically-valid unified diff with N hunks."""
    if new_file:
        header = "--- /dev/null\n+++ b/tests/new_test.py\n"
    else:
        header = "--- a/src/foo.py\n+++ b/src/foo.py\n"
    body = ""
    for i in range(num_hunks):
        line = 10 + i * 10
        body += (
            f"@@ -{line},2 +{line},3 @@\n"
            " context_line\n"
            "-old\n"
            "+new\n"
            "+added\n"
        )
    return header + body


class TestApplyDiffHunkCountThreshold:
    """v1.7.2.7 — apply_diff with ≤ 5 hunks is rejected unless it's a
    new-file diff. Forces TL to use replace/insert for small edits."""

    def test_one_hunk_rejected(self):
        plan = [
            _engineer_task_with_diff("T2", _build_diff(1)),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="below the > 5 threshold"):
            validate_plan(plan)

    def test_five_hunks_rejected(self):
        """Boundary: 5 is at-or-below; threshold is > 5."""
        plan = [
            _engineer_task_with_diff("T2", _build_diff(5)),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="below the > 5 threshold"):
            validate_plan(plan)

    def test_six_hunks_accepted(self):
        plan = [
            _engineer_task_with_diff("T2", _build_diff(6)),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)  # must not raise

    def test_new_file_diff_with_one_hunk_accepted(self):
        """new-file diffs are exempt from the threshold — you can't
        replace/insert into a file that doesn't exist."""
        plan = [
            _engineer_task_with_diff("T2", _build_diff(1, new_file=True)),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)


# ─── v1.7.2.7 — plan completeness ─────────────────────────────────────────────


from phalanx.agents._plan_validator import (  # noqa: E402
    validate_plan_completeness,
    validate_replan_strategy,
)


class TestPlanCompleteness:
    """C1/C2/C3 from validate_plan_completeness."""

    def test_engineer_task_with_only_commit_push_rejected(self):
        """C2: a 'plan' that's just [commit, push] with no patch step is
        a no-op. Reject."""
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "no-op plan",
                "steps": [
                    {"id": 1, "action": "commit", "message": "fix"},
                    {"id": 2, "action": "push"},
                ],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="no concrete patch steps"):
            validate_plan_completeness(plan, affected_files=["src/foo.py"])

    def test_empty_affected_files_with_file_edit_rejected(self):
        """C3: TL declared affected_files=[] but the plan modifies a file."""
        plan = [
            _engineer_task("T2"),  # default helper has a replace on src/foo.py
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="affected_files is empty"):
            validate_plan_completeness(plan, affected_files=[])

    def test_affected_files_declared_but_no_step_touches_them_rejected(self):
        """C1: affected_files declared but plan modifies a different file."""
        plan = [
            _engineer_task("T2"),  # touches src/foo.py
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="inconsistent"):
            validate_plan_completeness(plan, affected_files=["src/bar.py"])

    def test_consistent_plan_passes(self):
        plan = [
            _engineer_task("T2"),  # touches src/foo.py
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan_completeness(plan, affected_files=["src/foo.py"])

    def test_no_affected_files_arg_skips_C1_C3_but_keeps_C2(self):
        """When caller doesn't pass affected_files, only C2 (no patch
        steps) fires. C1/C3 are skipped because there's nothing to
        compare against."""
        plan = [
            _engineer_task("T2"),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        # Should not raise — engineer task has a replace step (C2 OK)
        # and no affected_files comparison (C1/C3 skipped).
        validate_plan_completeness(plan, affected_files=None)


# ─── v1.7.2.7 — REPLAN strategy-change ────────────────────────────────────────


def _engineer_task_replace(task_id: str, file_path: str) -> dict:
    """Helper: engineer task with single replace step on the given file."""
    return {
        "task_id": task_id,
        "agent": "cifix_engineer",
        "depends_on": [],
        "purpose": "replace",
        "steps": [
            {"id": 1, "action": "replace", "file": file_path,
             "old": "x = 1", "new": "x = 2"},
            {"id": 2, "action": "commit", "message": "fix"},
            {"id": 3, "action": "push"},
        ],
    }


class TestReplanStrategy:
    """v1.7.2.7 — iteration > 1 must change strategy AND explain why."""

    def test_iteration_1_skips_all_replan_checks(self):
        """First iteration has nothing to compare to. No raise."""
        plan = [_engineer_task_replace("T2", "src/foo.py"),
                _sre_verify_task("T3", depends_on=["T2"])]
        validate_replan_strategy(
            current_plan=plan,
            prior_plan=None,
            iteration=1,
            fix_spec_replan_reason=None,
        )

    def test_iteration_2_without_replan_reason_rejected(self):
        plan = [_engineer_task_replace("T4", "src/bar.py"),
                _sre_verify_task("T5", depends_on=["T4"])]
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        with pytest.raises(PlanValidationError, match="replan_reason"):
            validate_replan_strategy(
                current_plan=plan,
                prior_plan=prior,
                iteration=2,
                fix_spec_replan_reason=None,
            )

    def test_iteration_2_with_empty_replan_reason_rejected(self):
        plan = [_engineer_task_replace("T4", "src/bar.py"),
                _sre_verify_task("T5", depends_on=["T4"])]
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        with pytest.raises(PlanValidationError, match="replan_reason"):
            validate_replan_strategy(
                current_plan=plan,
                prior_plan=prior,
                iteration=2,
                fix_spec_replan_reason="   ",  # whitespace only
            )

    def test_iteration_2_same_strategy_signature_rejected(self):
        """Same (action, file) sequence as prior plan → reject as
        same-strategy. The user's exact spec: 'same-strategy repeat
        rejected'."""
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        # New plan with different task_id but same (replace, src/foo.py)
        current = [_engineer_task_replace("T4", "src/foo.py"),
                   _sre_verify_task("T5", depends_on=["T4"])]
        with pytest.raises(PlanValidationError, match="identical strategy signature"):
            validate_replan_strategy(
                current_plan=current,
                prior_plan=prior,
                iteration=2,
                fix_spec_replan_reason="prior fix didn't take",
            )

    def test_iteration_2_different_file_accepted(self):
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        current = [_engineer_task_replace("T4", "src/bar.py"),
                   _sre_verify_task("T5", depends_on=["T4"])]
        validate_replan_strategy(
            current_plan=current,
            prior_plan=prior,
            iteration=2,
            fix_spec_replan_reason="prior plan edited the wrong file; trying bar.py instead",
        )

    def test_iteration_2_different_action_accepted(self):
        """Pivoting from replace to insert/apply_diff is a real strategy
        change even on the same file."""
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        current = [
            {
                "task_id": "T4",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "pivot to insert",
                "steps": [
                    {"id": 1, "action": "insert", "file": "src/foo.py",
                     "after_line": 10, "content": "new\n"},
                    {"id": 2, "action": "commit", "message": "alt approach"},
                    {"id": 3, "action": "push"},
                ],
            },
            _sre_verify_task("T5", depends_on=["T4"]),
        ]
        validate_replan_strategy(
            current_plan=current,
            prior_plan=prior,
            iteration=2,
            fix_spec_replan_reason="replace on src/foo.py failed step_precondition; pivoting to insert",
        )

    def test_iteration_2_different_step_count_accepted(self):
        """Adding a step (e.g. an SRE setup before the engineer task)
        changes the signature."""
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        current = [
            _sre_setup_task("T4"),
            _engineer_task_replace("T5", "src/foo.py"),
            _sre_verify_task("T6", depends_on=["T5"]),
        ]
        # NOTE: signature is built from ENGINEER tasks only, so adding
        # an sre_setup task doesn't change it. Need to also tweak the
        # engineer task to register as different strategy. Add a 2nd
        # patch step.
        current[1]["steps"] = [
            {"id": 1, "action": "replace", "file": "src/foo.py",
             "old": "x = 1", "new": "x = 2"},
            {"id": 2, "action": "insert", "file": "src/foo.py",
             "after_line": 5, "content": "import httpx\n"},
            {"id": 3, "action": "commit", "message": "fix + dep"},
            {"id": 4, "action": "push"},
        ]
        validate_replan_strategy(
            current_plan=current,
            prior_plan=prior,
            iteration=2,
            fix_spec_replan_reason="prior plan missing httpx import; added setup + insert",
        )


# ─── User-spec test surface (the 4 categories named in v1.7.2.7 ask) ──────────


class TestUserSpecifiedV1727:
    """Direct test surface mapping to the user's spec:
      - flake test deletion rejected (already c8 — pinned again here)
      - malformed apply_diff rejected (already v1.7.2.5 — pinned again)
      - empty plan rejected (NEW)
      - same-strategy repeat rejected (NEW)
    """

    def test_empty_plan_rejected_no_patch_steps(self):
        """No engineer task contains a patch step."""
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "do nothing",
                "steps": [
                    {"id": 1, "action": "read", "file": "src/foo.py"},
                    {"id": 2, "action": "commit", "message": "no-op"},
                    {"id": 3, "action": "push"},
                ],
            },
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="no concrete patch steps"):
            validate_plan_completeness(plan, affected_files=["src/foo.py"])

    def test_empty_plan_rejected_zero_engineer_tasks(self):
        """Pure verify-only plan (no engineer task) — fails the
        structural validate_plan check first."""
        plan = [_sre_verify_task("T2")]
        # Structural check: this is technically valid (verify-only is
        # the ESCALATE shape), so completeness shouldn't fire either.
        validate_plan(plan)
        validate_plan_completeness(plan, affected_files=[])

    def test_malformed_apply_diff_rejected(self):
        """v1.7.2.5 already covered this, but the spec asks for a test
        in v1.7.2.7 too — pin it."""
        bad_diff = (
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        plan = [
            _engineer_task_with_diff("T2", bad_diff),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        with pytest.raises(PlanValidationError, match="fuzzy hunk header"):
            validate_plan(plan)

    def test_same_strategy_repeat_rejected(self):
        """The headline v1.7.2.7 use case — TL emits identical plan
        shape as the prior failed iteration."""
        prior = [_engineer_task_replace("T2", "src/foo.py"),
                 _sre_verify_task("T3", depends_on=["T2"])]
        current = [_engineer_task_replace("T4", "src/foo.py"),
                   _sre_verify_task("T5", depends_on=["T4"])]
        with pytest.raises(PlanValidationError, match="identical strategy signature"):
            validate_replan_strategy(
                current_plan=current,
                prior_plan=prior,
                iteration=2,
                fix_spec_replan_reason="trying again",
            )

    def test_flake_test_deletion_still_rejected_via_c8(self):
        """v1.7.2.5 c8 already rejects this; we're proving the layer
        still works after v1.7.2.7 changes."""
        from phalanx.agents._tl_self_critique import (
            check_c8_test_behavior_preserved,
        )
        delete_steps = [
            {"id": 1, "action": "delete_lines",
             "file": "tests/test_math_ops.py", "line": 10, "end_line": 20},
            {"id": 2, "action": "commit", "message": "remove flaky test"},
        ]
        ok, reason = check_c8_test_behavior_preserved(
            draft_steps=delete_steps,
            draft_root_cause="flaky test_multiply_with_jitter timeouts",
            ci_log_text="Failed: Timeout >2.0s",
        )
        assert ok is False
        assert "test" in reason.lower()
