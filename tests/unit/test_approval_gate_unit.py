"""
Unit tests for forge/workflow/approval_gate.py.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from forge.workflow.approval_gate import (
    ApprovalGate,
    ApprovalRejectedError,
    ApprovalTimeoutError,
)


def make_approval(
    approval_id="approval-1",
    gate_type="plan",
    gate_phase="planning",
    status="PENDING",
    decided_by=None,
    decision_note=None,
):
    approval = MagicMock()
    approval.id = approval_id
    approval.gate_type = gate_type
    approval.gate_phase = gate_phase
    approval.status = status
    approval.decided_by = decided_by
    approval.decision_note = decision_note
    return approval


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.expire_all = MagicMock()
    return session


@pytest.fixture
def gate(mock_session):
    return ApprovalGate(
        session=mock_session,
        run_id="run-1",
        slack_notify=False,  # disable Slack in unit tests
        timeout_seconds=60,
    )


class TestApprovalErrors:
    def test_rejected_error_message(self):
        err = ApprovalRejectedError("plan", note="Not ready")
        assert "plan" in str(err)
        assert "Not ready" in str(err)
        assert err.gate_type == "plan"
        assert err.note == "Not ready"

    def test_rejected_error_no_note(self):
        err = ApprovalRejectedError("ship")
        assert "no note" in str(err)

    def test_timeout_error_is_runtime_error(self):
        err = ApprovalTimeoutError("timed out")
        assert isinstance(err, RuntimeError)


class TestRequestAndWait:
    async def test_creates_approval_row(self, gate, mock_session):
        """Approval row is added to session and committed."""
        approval = make_approval(status="APPROVED")
        mock_session.refresh = AsyncMock(side_effect=lambda a: None)

        # After refresh, approval has an id; polling returns APPROVED
        poll_result = MagicMock()
        poll_result.scalar_one.return_value = approval
        mock_session.execute.return_value = poll_result

        with patch("forge.workflow.approval_gate.asyncio.sleep", AsyncMock()):
            result = await gate.request_and_wait(
                gate_type="plan",
                gate_phase="planning",
                context_snapshot={"plan_summary": "Build OAuth"},
            )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited()

    async def test_returns_approval_on_approved(self, gate, mock_session):
        approval = make_approval(status="APPROVED")
        mock_session.refresh = AsyncMock()

        # Set approval.id after refresh
        def set_id(a):
            a.id = "approval-123"
        mock_session.refresh.side_effect = set_id

        poll_result = MagicMock()
        poll_result.scalar_one.return_value = approval
        mock_session.execute.return_value = poll_result

        with patch("forge.workflow.approval_gate.asyncio.sleep", AsyncMock()):
            result = await gate.request_and_wait(gate_type="plan", gate_phase="planning")

        assert result.status == "APPROVED"

    async def test_raises_on_rejected(self, gate, mock_session):
        approval = make_approval(status="REJECTED", decision_note="Plan is wrong")
        mock_session.refresh = AsyncMock()

        def set_id(a):
            a.id = "approval-123"
        mock_session.refresh.side_effect = set_id

        poll_result = MagicMock()
        poll_result.scalar_one.return_value = approval
        mock_session.execute.return_value = poll_result

        with (
            patch("forge.workflow.approval_gate.asyncio.sleep", AsyncMock()),
            pytest.raises(ApprovalRejectedError, match="plan"),
        ):
            await gate.request_and_wait(gate_type="plan", gate_phase="planning")

    async def test_timeout_raises_timeout_error(self, mock_session):
        """When no decision arrives before timeout, raises ApprovalTimeoutError."""
        gate = ApprovalGate(
            session=mock_session,
            run_id="run-1",
            slack_notify=False,
            timeout_seconds=30,  # 30s timeout
        )

        approval = make_approval(status="PENDING")
        mock_session.refresh = AsyncMock()

        def set_id(a):
            a.id = "approval-123"
        mock_session.refresh.side_effect = set_id

        poll_result = MagicMock()
        poll_result.scalar_one.return_value = approval  # always PENDING
        mock_session.execute.return_value = poll_result

        with (
            patch("forge.workflow.approval_gate.asyncio.sleep", AsyncMock()),
            patch("forge.workflow.approval_gate._POLL_INTERVAL_SECONDS", 31),  # force timeout
            pytest.raises(ApprovalTimeoutError),
        ):
            await gate.request_and_wait(gate_type="plan", gate_phase="planning")


class TestSlackNotify:
    async def test_notify_skipped_when_no_token(self, mock_session):
        gate = ApprovalGate(session=mock_session, run_id="r1", slack_notify=True)
        approval = make_approval()

        with patch("forge.config.settings.get_settings") as mock_settings:
            mock_settings.return_value.slack_bot_token = None
            # Should complete without raising
            await gate._notify_slack(approval, context={"plan_summary": "Test"})

    async def test_notify_handles_exception_gracefully(self, mock_session):
        """Slack notify failure must not abort the gate."""
        gate = ApprovalGate(session=mock_session, run_id="r1", slack_notify=True)
        approval = make_approval()

        with patch("forge.config.settings.get_settings", side_effect=Exception("no settings")):
            # Should NOT raise
            await gate._notify_slack(approval, context=None)
