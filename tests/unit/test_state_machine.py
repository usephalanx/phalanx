"""
Unit tests for the FORGE Run state machine.

These are pure Python tests — no DB, no I/O, no async.
The state machine is a pure function module so tests are fast and deterministic.

Coverage targets:
  - All happy-path transitions
  - All approval rejection transitions
  - All failure transitions from active states
  - All cancel transitions
  - All pause/resume transitions
  - Terminal state enforcement (no exit from SHIPPED/FAILED/CANCELLED)
  - allowed_next_states() completeness
  - is_valid_transition() boolean helper
"""

import pytest

from forge.workflow.state_machine import (
    INTERRUPTIBLE_STATES,
    TERMINAL_STATES,
    InvalidTransitionError,
    RunStatus,
    TerminalStateError,
    allowed_next_states,
    is_valid_transition,
    validate_transition,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def assert_valid(from_s: str, to_s: str) -> None:
    validate_transition(RunStatus(from_s), RunStatus(to_s))


def assert_invalid(from_s: str, to_s: str) -> None:
    with pytest.raises((InvalidTransitionError, TerminalStateError)):
        validate_transition(RunStatus(from_s), RunStatus(to_s))


# ---------------------------------------------------------------------------
# Happy path (linear forward progression)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_intake_to_researching(self):
        assert_valid("INTAKE", "RESEARCHING")

    def test_researching_to_planning(self):
        assert_valid("RESEARCHING", "PLANNING")

    def test_planning_to_awaiting_plan_approval(self):
        assert_valid("PLANNING", "AWAITING_PLAN_APPROVAL")

    def test_plan_approved_to_executing(self):
        assert_valid("AWAITING_PLAN_APPROVAL", "EXECUTING")

    def test_executing_to_verifying(self):
        assert_valid("EXECUTING", "VERIFYING")

    def test_verifying_to_awaiting_ship_approval(self):
        assert_valid("VERIFYING", "AWAITING_SHIP_APPROVAL")

    def test_ship_approved_to_ready_to_merge(self):
        assert_valid("AWAITING_SHIP_APPROVAL", "READY_TO_MERGE")

    def test_ready_to_merge_to_merged(self):
        assert_valid("READY_TO_MERGE", "MERGED")

    def test_merged_to_release_prep(self):
        assert_valid("MERGED", "RELEASE_PREP")

    def test_release_prep_to_awaiting_release_approval(self):
        assert_valid("RELEASE_PREP", "AWAITING_RELEASE_APPROVAL")

    def test_release_approved_to_shipped(self):
        assert_valid("AWAITING_RELEASE_APPROVAL", "SHIPPED")

    def test_full_happy_path_sequence(self):
        """Validate the entire forward chain end-to-end."""
        path = [
            "INTAKE",
            "RESEARCHING",
            "PLANNING",
            "AWAITING_PLAN_APPROVAL",
            "EXECUTING",
            "VERIFYING",
            "AWAITING_SHIP_APPROVAL",
            "READY_TO_MERGE",
            "MERGED",
            "RELEASE_PREP",
            "AWAITING_RELEASE_APPROVAL",
            "SHIPPED",
        ]
        for i in range(len(path) - 1):
            assert_valid(path[i], path[i + 1])


# ---------------------------------------------------------------------------
# Approval rejections
# ---------------------------------------------------------------------------


class TestApprovalRejections:
    def test_plan_rejected_returns_to_planning(self):
        assert_valid("AWAITING_PLAN_APPROVAL", "PLANNING")

    def test_ship_rejected_returns_to_executing(self):
        assert_valid("AWAITING_SHIP_APPROVAL", "EXECUTING")

    def test_release_rejected_returns_to_release_prep(self):
        assert_valid("AWAITING_RELEASE_APPROVAL", "RELEASE_PREP")

    def test_plan_approval_cannot_skip_to_merged(self):
        assert_invalid("AWAITING_PLAN_APPROVAL", "MERGED")

    def test_ship_approval_cannot_jump_to_shipped(self):
        assert_invalid("AWAITING_SHIP_APPROVAL", "SHIPPED")


# ---------------------------------------------------------------------------
# Failure transitions
# ---------------------------------------------------------------------------


class TestFailureTransitions:
    FAIL_FROM = [
        "RESEARCHING",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "AWAITING_SHIP_APPROVAL",
        "READY_TO_MERGE",
        "RELEASE_PREP",
        "AWAITING_RELEASE_APPROVAL",
    ]

    @pytest.mark.parametrize("from_state", FAIL_FROM)
    def test_can_fail_from_active_state(self, from_state: str):
        assert_valid(from_state, "FAILED")

    def test_cannot_fail_from_intake(self):
        # INTAKE → FAILED is not a defined path; should go RESEARCHING first
        assert_invalid("INTAKE", "FAILED")

    def test_cannot_fail_from_shipped(self):
        assert_invalid("SHIPPED", "FAILED")

    def test_cannot_fail_from_cancelled(self):
        assert_invalid("CANCELLED", "FAILED")

    def test_cannot_fail_from_blocked(self):
        # BLOCKED → FAILED is valid per spec (stuck too long)
        assert_valid("BLOCKED", "FAILED")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    CANCEL_FROM = [
        "INTAKE",
        "RESEARCHING",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "AWAITING_SHIP_APPROVAL",
        "READY_TO_MERGE",
        "MERGED",
        "RELEASE_PREP",
        "AWAITING_RELEASE_APPROVAL",
        "BLOCKED",
        "PAUSED",
    ]

    @pytest.mark.parametrize("from_state", CANCEL_FROM)
    def test_can_cancel_from_any_active_state(self, from_state: str):
        assert_valid(from_state, "CANCELLED")

    def test_cannot_cancel_from_shipped(self):
        assert_invalid("SHIPPED", "CANCELLED")

    def test_cannot_cancel_from_failed(self):
        assert_invalid("FAILED", "CANCELLED")

    def test_cannot_cancel_from_cancelled(self):
        assert_invalid("CANCELLED", "CANCELLED")


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


class TestBlocking:
    def test_can_block_from_researching(self):
        assert_valid("RESEARCHING", "BLOCKED")

    def test_can_block_from_planning(self):
        assert_valid("PLANNING", "BLOCKED")

    def test_can_block_from_executing(self):
        assert_valid("EXECUTING", "BLOCKED")

    def test_can_block_from_verifying(self):
        assert_valid("VERIFYING", "BLOCKED")

    def test_cannot_block_from_intake(self):
        assert_invalid("INTAKE", "BLOCKED")

    def test_cannot_block_from_awaiting_plan_approval(self):
        # Approval states pause, not block
        assert_invalid("AWAITING_PLAN_APPROVAL", "BLOCKED")

    def test_unblock_to_executing(self):
        assert_valid("BLOCKED", "EXECUTING")

    def test_unblock_to_planning(self):
        assert_valid("BLOCKED", "PLANNING")

    def test_blocked_cannot_jump_to_shipped(self):
        assert_invalid("BLOCKED", "SHIPPED")


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    PAUSE_FROM = [
        "RESEARCHING",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "AWAITING_SHIP_APPROVAL",
    ]

    @pytest.mark.parametrize("from_state", PAUSE_FROM)
    def test_can_pause_from_active_state(self, from_state: str):
        assert_valid(from_state, "PAUSED")

    RESUME_TO = [
        "RESEARCHING",
        "PLANNING",
        "EXECUTING",
        "VERIFYING",
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_SHIP_APPROVAL",
    ]

    @pytest.mark.parametrize("to_state", RESUME_TO)
    def test_can_resume_to_prior_state(self, to_state: str):
        assert_valid("PAUSED", to_state)

    def test_cannot_resume_to_shipped(self):
        assert_invalid("PAUSED", "SHIPPED")

    def test_cannot_resume_to_merged(self):
        assert_invalid("PAUSED", "MERGED")

    def test_paused_can_be_cancelled(self):
        assert_valid("PAUSED", "CANCELLED")


# ---------------------------------------------------------------------------
# Terminal state enforcement
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_terminal_states_are_correct(self):
        assert RunStatus.SHIPPED in TERMINAL_STATES
        assert RunStatus.FAILED in TERMINAL_STATES
        assert RunStatus.CANCELLED in TERMINAL_STATES

    @pytest.mark.parametrize("terminal", ["SHIPPED", "FAILED", "CANCELLED"])
    def test_no_transition_out_of_terminal(self, terminal: str):
        with pytest.raises(TerminalStateError):
            validate_transition(RunStatus(terminal), RunStatus.EXECUTING)

    @pytest.mark.parametrize("terminal", ["SHIPPED", "FAILED", "CANCELLED"])
    def test_terminal_has_no_allowed_next_states(self, terminal: str):
        assert allowed_next_states(RunStatus(terminal)) == frozenset()


# ---------------------------------------------------------------------------
# Illegal jumps (non-adjacent state skips)
# ---------------------------------------------------------------------------


class TestIllegalJumps:
    def test_cannot_jump_intake_to_executing(self):
        assert_invalid("INTAKE", "EXECUTING")

    def test_cannot_jump_researching_to_shipped(self):
        assert_invalid("RESEARCHING", "SHIPPED")

    def test_cannot_jump_planning_to_merged(self):
        assert_invalid("PLANNING", "MERGED")

    def test_cannot_jump_executing_to_release_prep(self):
        assert_invalid("EXECUTING", "RELEASE_PREP")

    def test_cannot_go_backwards_executing_to_intake(self):
        assert_invalid("EXECUTING", "INTAKE")

    def test_cannot_go_backwards_verifying_to_researching(self):
        assert_invalid("VERIFYING", "RESEARCHING")

    def test_cannot_stay_in_same_state(self):
        for state in RunStatus:
            if state not in TERMINAL_STATES:
                assert_invalid(state, state)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_is_valid_transition_returns_true(self):
        assert is_valid_transition(RunStatus.INTAKE, RunStatus.RESEARCHING) is True

    def test_is_valid_transition_returns_false(self):
        assert is_valid_transition(RunStatus.INTAKE, RunStatus.SHIPPED) is False

    def test_is_valid_transition_terminal_returns_false(self):
        assert is_valid_transition(RunStatus.SHIPPED, RunStatus.EXECUTING) is False

    def test_allowed_next_states_from_intake(self):
        allowed = allowed_next_states(RunStatus.INTAKE)
        assert RunStatus.RESEARCHING in allowed
        assert RunStatus.CANCELLED in allowed
        # Should NOT include SHIPPED, FAILED, EXECUTING, etc.
        assert RunStatus.SHIPPED not in allowed
        assert RunStatus.EXECUTING not in allowed

    def test_allowed_next_states_from_executing(self):
        allowed = allowed_next_states(RunStatus.EXECUTING)
        assert RunStatus.VERIFYING in allowed
        assert RunStatus.FAILED in allowed
        assert RunStatus.BLOCKED in allowed
        assert RunStatus.PAUSED in allowed
        assert RunStatus.CANCELLED in allowed

    def test_allowed_next_states_is_consistent_with_validate(self):
        """Every state in allowed_next_states() must pass validate_transition()."""
        for from_state in RunStatus:
            if from_state in TERMINAL_STATES:
                continue
            for to_state in allowed_next_states(from_state):
                # Should not raise
                validate_transition(from_state, to_state)

    def test_interruptible_states(self):
        assert RunStatus.RESEARCHING in INTERRUPTIBLE_STATES
        assert RunStatus.EXECUTING in INTERRUPTIBLE_STATES
        assert RunStatus.SHIPPED not in INTERRUPTIBLE_STATES
        assert RunStatus.AWAITING_PLAN_APPROVAL not in INTERRUPTIBLE_STATES
