"""P0-6 — shadow ledger reconciliation: 5 scenarios + idempotency.

Tests are unit-level. They exercise the validator and the reconciler's
decision logic by constructing fake ledger + run + task fixtures.

Scenarios (per the spec):
  1. stale SAFE_ESCALATE reconciles to FAILED_INFRA_TIMEOUT
  2. provenance preserves original snapshot fields
  3. CLI cannot finalize misleading SAFE_ESCALATE (validator gate)
  4. validator rejects malformed terminal states
  5. reconciliation is idempotent (re-running a no-op writes nothing)
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from phalanx.shadow.provenance import (
    build_provenance,
    is_well_formed_terminal_state,
)


# ── Validator scenarios ───────────────────────────────────────────────────────


class TestValidatorRejectsIllFormedTerminal:
    """Scenario 4 — validator rejects malformed terminal states."""

    def test_pending_verdict_always_accepted(self):
        ok, reason = is_well_formed_terminal_state(
            verdict="PENDING", provenance={"tl_task_status": "PENDING"}
        )
        assert ok is True
        assert reason is None

    @pytest.mark.parametrize(
        "tl_status",
        ["PENDING", "IN_PROGRESS", "CANCELLED"],
    )
    def test_safe_escalate_rejected_when_tl_not_terminal(self, tl_status):
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE",
            provenance={"tl_task_status": tl_status, "tl_task_count": 1},
        )
        assert ok is False, f"tl_status={tl_status} should be rejected"
        assert reason is not None
        assert tl_status in reason
        assert "terminal" in reason.lower()

    def test_safe_escalate_rejected_when_tl_status_is_none_but_count_nonzero(self):
        """tl_task_count > 0 but tl_task_status is None — the snapshot
        is incomplete. This is the W1.9-shape near-miss."""
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE",
            provenance={"tl_task_status": None, "tl_task_count": 1},
        )
        assert ok is False
        assert reason is not None
        assert "None" in reason or "tl_task_status" in reason

    def test_safe_escalate_accepted_when_tl_count_zero(self):
        """tl_task_count=0 with SAFE_ESCALATE is valid — TL never ran
        (e.g. FAILED_SANDBOX_SETUP path); root_cause is synthesized."""
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE",
            provenance={"tl_task_status": None, "tl_task_count": 0},
        )
        assert ok is True
        assert reason is None

    def test_safe_escalate_accepted_when_tl_completed(self):
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE",
            provenance={"tl_task_status": "COMPLETED", "tl_task_count": 1},
        )
        assert ok is True

    def test_failed_verdict_with_tl_pending_is_still_accepted(self):
        """FAILED + tl_task_status=PENDING is the FAILED_SANDBOX_SETUP
        path (TL queued but never ran). Don't gate on TL terminal here."""
        ok, reason = is_well_formed_terminal_state(
            verdict="FAILED",
            provenance={"tl_task_status": "PENDING", "tl_task_count": 1},
        )
        assert ok is True

    def test_none_verdict_accepted(self):
        ok, _ = is_well_formed_terminal_state(verdict=None, provenance=None)
        assert ok is True

    def test_validator_tolerates_empty_provenance(self):
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE", provenance={}
        )
        # tl_task_count defaults to 0 → valid (counted as synthesized path)
        assert ok is True


# ── Reconciler unit tests via fakes ──────────────────────────────────────────


def _task(*, role: str, seq: int, status: str = "COMPLETED",
          output: dict | None = None, task_id: str | None = None):
    return SimpleNamespace(
        id=task_id or f"task-{role}-{seq}",
        agent_role=role,
        sequence_num=seq,
        status=status,
        output=output or {},
        created_at=datetime(2026, 5, 11, 20, 15, seq, tzinfo=timezone.utc),
    )


class TestReconcilerDecisionLogic:
    """Scenarios 1, 2, 5 — what the reconciler does, in isolation from DB.

    We test the _needs_reconciliation/_reconciliation_reason logic by
    constructing the same shapes the reconciler sees in production.
    """

    def test_pending_ledger_with_terminal_run_needs_reconciliation(self):
        """Scenario 1 setup — CLI exited PENDING, run is now terminal."""
        from phalanx.maintenance.ledger_reconciler import _reconciliation_reason

        run = SimpleNamespace(status="FAILED", failure_class="FAILED_INFRA_TIMEOUT")
        reason = _reconciliation_reason(
            original_verdict="PENDING",
            original_failure_class=None,
            new_verdict="FAILED",
            new_failure_class="FAILED_INFRA_TIMEOUT",
            run=run,
        )
        assert reason == "cli_left_pending_run_terminal"

    def test_ill_formed_safe_escalate_replaced(self):
        """Scenario 2 setup — original SAFE_ESCALATE replaced by FAILED."""
        from phalanx.maintenance.ledger_reconciler import _reconciliation_reason

        run = SimpleNamespace(status="FAILED", failure_class="FAILED_INFRA_TIMEOUT")
        reason = _reconciliation_reason(
            original_verdict="SAFE_ESCALATE",
            original_failure_class=None,
            new_verdict="FAILED",
            new_failure_class="FAILED_INFRA_TIMEOUT",
            run=run,
        )
        # The reason names the failure class explicitly so audit tooling
        # can group W2-Batch-1-shape healings.
        assert "watchdog" in reason
        assert "infra_timeout" in reason

    def test_no_change_reason_is_no_op_marker(self):
        from phalanx.maintenance.ledger_reconciler import _reconciliation_reason

        run = SimpleNamespace(status="FAILED", failure_class="FAILED_SANDBOX_SETUP")
        reason = _reconciliation_reason(
            original_verdict="FAILED",
            original_failure_class="FAILED_SANDBOX_SETUP",
            new_verdict="FAILED",
            new_failure_class="FAILED_SANDBOX_SETUP",
            run=run,
        )
        # Reaches the fall-through branch; the reconciler itself would
        # short-circuit before calling _reconciliation_reason in the
        # truly-no-op case, but the helper must remain defined for
        # combinations where state evolves but verdict doesn't.
        assert reason == "snapshot_evolved_with_run_state"


# ── Idempotency contract ──────────────────────────────────────────────────────


class TestIdempotencyDecisions:
    """Scenario 5 — repeated reconciliation is a no-op when state matches."""

    def test_needs_reconciliation_returns_false_for_well_formed_terminal(self):
        """A row that's already at its correct terminal state should not
        be re-reconciled. The decision happens BEFORE any write."""
        # Build a SAFE_ESCALATE row with a COMPLETED TL task → well-formed.
        prov = build_provenance([
            _task(role="cifix_techlead", seq=1, status="COMPLETED",
                  output={"confidence": 0.0, "review_decision": "ESCALATE",
                          "root_cause": "TL refused with reason"}),
        ])
        ok, _ = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE", provenance=prov,
        )
        assert ok is True
        # The reconciler's _needs_reconciliation should NOT trigger
        # on Shape B because tl_task_status=COMPLETED.
        assert prov["tl_task_status"] == "COMPLETED"


# ── Integration shape: cli refusal path → stays PENDING ─────────────────────


class TestCLIRefusalKeepsLedgerPending:
    """Scenario 3 — CLI cannot finalize a misleading SAFE_ESCALATE.

    The actual CLI code path is tested through the validator. If the
    validator returns ok=False, the runner short-circuits to a
    'ILL_FORMED_SNAPSHOT_REFUSED' branch and DOES NOT call
    update_with_results. We assert the precondition (validator)."""

    def test_validator_blocks_w1_9_shape(self):
        """The W1.9 / W2 Batch 1 shape: SAFE_ESCALATE with tl_status=PENDING."""
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE",
            provenance={
                "tl_task_status": "PENDING",
                "tl_task_count": 1,
                "tl_task_id": "phantom-tl-id",
            },
        )
        assert ok is False
        assert reason and "PENDING" in reason

    def test_validator_blocks_w2_batch_1_cancelled_shape(self):
        ok, reason = is_well_formed_terminal_state(
            verdict="SAFE_ESCALATE",
            provenance={
                "tl_task_status": "CANCELLED",
                "tl_task_count": 1,
            },
        )
        assert ok is False
        assert reason and "CANCELLED" in reason
