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
