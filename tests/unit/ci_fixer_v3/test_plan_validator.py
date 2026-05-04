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
        """Common shape — TL emits a unified diff for engineer to apply."""
        plan = [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "steps": [
                    {
                        "id": 1,
                        "action": "apply_diff",
                        "diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
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


_VALID_DIFF = """\
--- a/tests/test_math_ops.py
+++ b/tests/test_math_ops.py
@@ -1,5 +1,9 @@
 from calc.math_ops import add, divide, multiply, subtract
+
+
+def test_percentage():
+    assert percentage(1, 4) == 25.0
"""

_VALID_DIFF_NO_COUNT = """\
--- a/src/x.py
+++ b/src/x.py
@@ -12 +12 @@
-x = 1
+x = 2
"""

_VALID_DIFF_WITH_CONTEXT_HEADER = """\
--- a/src/x.py
+++ b/src/x.py
@@ -10,7 +10,8 @@ def existing_function():
     return 42

+
+def new_function():
+    return 43
 def another():
     pass
"""

_VALID_DIFF_NEW_FILE = """\
--- /dev/null
+++ b/tests/new_test.py
@@ -0,0 +1,4 @@
+def test_new():
+    assert True
+
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

    def test_valid_unified_diff_accepted(self):
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)  # must not raise

    def test_valid_diff_no_line_count_accepted(self):
        """`@@ -L +L @@` (single-line, count omitted) is valid git format."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_NO_COUNT),
            _sre_verify_task("T3", depends_on=["T2"]),
        ]
        validate_plan(plan)

    def test_valid_diff_with_context_header_accepted(self):
        """`@@ -L,N +L,N @@ def existing_function():` (with trailing
        context after the second `@@`) is valid."""
        plan = [
            _engineer_task_with_diff("T2", _VALID_DIFF_WITH_CONTEXT_HEADER),
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
        in hunk headers), the validator accepts. Mirrors the expected
        outcome of the validator's force-replan signal."""
        corrected = (
            "--- a/tests/test_math_ops.py\n"
            "+++ b/tests/test_math_ops.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-from calc.math_ops import add, divide, multiply, subtract\n"
            "+from calc.math_ops import add, average, divide, multiply, percentage, subtract\n"
            "@@ -10,3 +10,12 @@ def test_divide_by_zero_raises():\n"
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
            _engineer_task_with_diff("T2", corrected),
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
