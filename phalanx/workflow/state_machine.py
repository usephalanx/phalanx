"""
Run state machine — single source of truth for all valid state transitions.

This module is intentionally pure Python with no I/O. DB persistence is the
caller's responsibility. This makes the state machine trivially unit-testable.

Valid states (mirrors ck_run_valid_status in models.py):
    INTAKE → RESEARCHING → PLANNING → AWAITING_PLAN_APPROVAL
    → EXECUTING → VERIFYING → AWAITING_SHIP_APPROVAL
    → READY_TO_MERGE → MERGED → RELEASE_PREP
    → AWAITING_RELEASE_APPROVAL → SHIPPED
    Any active state → FAILED | BLOCKED | PAUSED | CANCELLED
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    INTAKE = "INTAKE"
    RESEARCHING = "RESEARCHING"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    AWAITING_SHIP_APPROVAL = "AWAITING_SHIP_APPROVAL"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    RELEASE_PREP = "RELEASE_PREP"
    AWAITING_RELEASE_APPROVAL = "AWAITING_RELEASE_APPROVAL"
    SHIPPED = "SHIPPED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


# Terminal states — no transitions out of these
TERMINAL_STATES: frozenset[RunStatus] = frozenset(
    {RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED}
)

# States that can be interrupted (preempted by P0/P1 incidents)
INTERRUPTIBLE_STATES: frozenset[RunStatus] = frozenset(
    {RunStatus.RESEARCHING, RunStatus.PLANNING, RunStatus.EXECUTING, RunStatus.VERIFYING}
)

# The complete transition graph — (from, to) pairs that are explicitly permitted.
# Any pair NOT in this set is INVALID regardless of actor or context.
_ALLOWED_TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    [
        # Happy path
        (RunStatus.INTAKE, RunStatus.RESEARCHING),
        (RunStatus.RESEARCHING, RunStatus.PLANNING),
        (RunStatus.PLANNING, RunStatus.AWAITING_PLAN_APPROVAL),
        (RunStatus.AWAITING_PLAN_APPROVAL, RunStatus.EXECUTING),  # plan approved
        (RunStatus.EXECUTING, RunStatus.VERIFYING),
        (RunStatus.VERIFYING, RunStatus.AWAITING_SHIP_APPROVAL),
        # CI Fixer v3 iteration: when cifix_sre (verify mode) reports
        # new_failures, cifix_commander inserts another round of tasks and
        # rewinds to EXECUTING. The guard is policy-level (commander only
        # rewinds for run_type='ci_fix' with iteration_count below the cap)
        # — the state machine just allows the edge. Build flow never takes
        # this path because its commander drives VERIFYING → SHIP_APPROVAL.
        (RunStatus.VERIFYING, RunStatus.EXECUTING),
        (RunStatus.AWAITING_SHIP_APPROVAL, RunStatus.READY_TO_MERGE),  # ship approved
        (RunStatus.READY_TO_MERGE, RunStatus.MERGED),
        (RunStatus.MERGED, RunStatus.RELEASE_PREP),
        (RunStatus.RELEASE_PREP, RunStatus.AWAITING_RELEASE_APPROVAL),
        (RunStatus.AWAITING_RELEASE_APPROVAL, RunStatus.SHIPPED),  # release approved
        # Approval rejections → back to prior work state
        (RunStatus.AWAITING_PLAN_APPROVAL, RunStatus.PLANNING),  # plan rejected
        (RunStatus.AWAITING_SHIP_APPROVAL, RunStatus.EXECUTING),  # ship rejected → rework
        (RunStatus.AWAITING_RELEASE_APPROVAL, RunStatus.RELEASE_PREP),  # release rejected
        # Failure from any active state
        (RunStatus.RESEARCHING, RunStatus.FAILED),
        (RunStatus.PLANNING, RunStatus.FAILED),
        (RunStatus.AWAITING_PLAN_APPROVAL, RunStatus.FAILED),
        (RunStatus.EXECUTING, RunStatus.FAILED),
        (RunStatus.VERIFYING, RunStatus.FAILED),
        (RunStatus.AWAITING_SHIP_APPROVAL, RunStatus.FAILED),
        (RunStatus.READY_TO_MERGE, RunStatus.FAILED),
        (RunStatus.RELEASE_PREP, RunStatus.FAILED),
        (RunStatus.AWAITING_RELEASE_APPROVAL, RunStatus.FAILED),
        # Blocking (external dependency / human hold)
        (RunStatus.RESEARCHING, RunStatus.BLOCKED),
        (RunStatus.PLANNING, RunStatus.BLOCKED),
        (RunStatus.EXECUTING, RunStatus.BLOCKED),
        (RunStatus.VERIFYING, RunStatus.BLOCKED),
        (RunStatus.BLOCKED, RunStatus.EXECUTING),  # unblocked
        (RunStatus.BLOCKED, RunStatus.PLANNING),  # unblocked to earlier phase
        (RunStatus.BLOCKED, RunStatus.FAILED),
        # Pause/resume (human-initiated or interrupt handler)
        (RunStatus.RESEARCHING, RunStatus.PAUSED),
        (RunStatus.PLANNING, RunStatus.PAUSED),
        (RunStatus.EXECUTING, RunStatus.PAUSED),
        (RunStatus.VERIFYING, RunStatus.PAUSED),
        (RunStatus.AWAITING_PLAN_APPROVAL, RunStatus.PAUSED),
        (RunStatus.AWAITING_SHIP_APPROVAL, RunStatus.PAUSED),
        (RunStatus.PAUSED, RunStatus.RESEARCHING),
        (RunStatus.PAUSED, RunStatus.PLANNING),
        (RunStatus.PAUSED, RunStatus.EXECUTING),
        (RunStatus.PAUSED, RunStatus.VERIFYING),
        (RunStatus.PAUSED, RunStatus.AWAITING_PLAN_APPROVAL),
        (RunStatus.PAUSED, RunStatus.AWAITING_SHIP_APPROVAL),
        (RunStatus.PAUSED, RunStatus.CANCELLED),
        # Cancellation from any non-terminal state
        (RunStatus.INTAKE, RunStatus.CANCELLED),
        (RunStatus.RESEARCHING, RunStatus.CANCELLED),
        (RunStatus.PLANNING, RunStatus.CANCELLED),
        (RunStatus.AWAITING_PLAN_APPROVAL, RunStatus.CANCELLED),
        (RunStatus.EXECUTING, RunStatus.CANCELLED),
        (RunStatus.VERIFYING, RunStatus.CANCELLED),
        (RunStatus.AWAITING_SHIP_APPROVAL, RunStatus.CANCELLED),
        (RunStatus.READY_TO_MERGE, RunStatus.CANCELLED),
        (RunStatus.MERGED, RunStatus.CANCELLED),
        (RunStatus.RELEASE_PREP, RunStatus.CANCELLED),
        (RunStatus.AWAITING_RELEASE_APPROVAL, RunStatus.CANCELLED),
        (RunStatus.BLOCKED, RunStatus.CANCELLED),
    ]
)


class InvalidTransitionError(ValueError):
    """Raised when a transition is not permitted by the state machine."""

    def __init__(self, from_state: RunStatus, to_state: RunStatus) -> None:
        super().__init__(
            f"Invalid transition: {from_state} → {to_state}. "
            f"This transition is not permitted by the FORGE state machine."
        )
        self.from_state = from_state
        self.to_state = to_state


class TerminalStateError(ValueError):
    """Raised when attempting to transition out of a terminal state."""

    def __init__(self, state: RunStatus) -> None:
        super().__init__(
            f"Cannot transition from terminal state '{state}'. "
            f"Terminal states are: {sorted(TERMINAL_STATES)}"
        )
        self.state = state


def validate_transition(from_state: RunStatus, to_state: RunStatus) -> None:
    """
    Assert that a state transition is valid.
    Raises InvalidTransitionError or TerminalStateError on failure.
    This is a pure function — no side effects.
    """
    if from_state in TERMINAL_STATES:
        raise TerminalStateError(from_state)

    if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(from_state, to_state)


def is_valid_transition(from_state: RunStatus, to_state: RunStatus) -> bool:
    """Boolean check — does not raise."""
    if from_state in TERMINAL_STATES:
        return False
    return (from_state, to_state) in _ALLOWED_TRANSITIONS


def allowed_next_states(from_state: RunStatus) -> frozenset[RunStatus]:
    """Return all valid next states from the given state."""
    if from_state in TERMINAL_STATES:
        return frozenset()
    return frozenset(to for (frm, to) in _ALLOWED_TRANSITIONS if frm == from_state)
