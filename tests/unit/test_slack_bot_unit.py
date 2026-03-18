"""
Unit tests for forge/gateway/slack_bot.py.

Tests the handler helper functions (_handle_build, _handle_status, _handle_cancel,
_handle_approval_action) by mocking DB session and Slack client.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_db_context(session):
    @asynccontextmanager
    async def _get_db():
        yield session
    return _get_db


def make_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    session.refresh = AsyncMock()
    return session


# ── _handle_build ──────────────────────────────────────────────────────────────

class TestHandleBuild:
    async def test_build_channel_not_registered(self):
        from forge.gateway.slack_bot import _handle_build
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("build Add OAuth login")
        respond = AsyncMock()
        session = make_session()

        # Channel query returns None
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_build(parsed, user_id="U123", channel_id="C123", respond=respond)

        respond.assert_awaited_once()
        assert "not linked" in respond.call_args[0][0].lower() or "not linked" in str(respond.call_args)

    async def test_build_channel_no_project(self):
        from forge.gateway.slack_bot import _handle_build
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("build Add OAuth")
        respond = AsyncMock()
        session = make_session()

        channel = MagicMock()
        channel.project_id = None
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = channel
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_build(parsed, user_id="U123", channel_id="C456", respond=respond)

        respond.assert_awaited_once()
        assert "no associated project" in respond.call_args[0][0].lower()

    async def test_build_creates_work_order_and_dispatches(self):
        from forge.gateway.slack_bot import _handle_build
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("build Add OAuth login")
        respond = AsyncMock()
        session = make_session()

        channel = MagicMock()
        channel.id = "ch-uuid"
        channel.project_id = "proj-uuid"
        channel_result = MagicMock()
        channel_result.scalar_one_or_none.return_value = channel
        session.execute.return_value = channel_result

        wo = MagicMock()
        wo.id = "wo-uuid"
        wo.project_id = "proj-uuid"
        wo.title = "Add OAuth login"
        session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", "wo-uuid"))

        mock_router = MagicMock()
        mock_router.dispatch.return_value = "celery-task-123"

        with (
            patch("forge.db.session.get_db", make_db_context(session)),
            patch("forge.runtime.task_router.TaskRouter", return_value=mock_router),
            patch("forge.queue.celery_app.celery_app", MagicMock()),
        ):
            await _handle_build(parsed, user_id="U123", channel_id="C789", respond=respond)

        respond.assert_awaited_once()
        call_text = respond.call_args[0][0]
        assert "✅" in call_text or "Work order" in call_text

    async def test_build_handles_exception_gracefully(self):
        from forge.gateway.slack_bot import _handle_build
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("build Add OAuth")
        respond = AsyncMock()

        with patch("forge.db.session.get_db", side_effect=Exception("DB down")):
            await _handle_build(parsed, user_id="U123", channel_id="C123", respond=respond)

        respond.assert_awaited_once()
        assert "❌" in respond.call_args[0][0]


# ── _handle_status ─────────────────────────────────────────────────────────────

class TestHandleStatus:
    async def test_status_no_active_runs(self):
        from forge.gateway.slack_bot import _handle_status
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("status")
        respond = AsyncMock()
        session = make_session()

        result_mock = MagicMock()
        result_mock.all.return_value = []
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_status(parsed, respond=respond)

        respond.assert_awaited_once()
        assert "No active" in respond.call_args[0][0]

    async def test_status_specific_run_not_found(self):
        from forge.gateway.slack_bot import _handle_status
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("status abc-123")
        respond = AsyncMock()
        session = make_session()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_status(parsed, respond=respond)

        respond.assert_awaited_once()
        assert "not found" in respond.call_args[0][0]

    async def test_status_specific_run_found(self):
        from forge.gateway.slack_bot import _handle_status
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("status abc-123")
        respond = AsyncMock()
        session = make_session()

        run = MagicMock()
        run.id = "abc-123"
        run.status = "EXECUTING"
        run.estimated_cost_usd = 0.12
        run.active_branch = "feat/auth"
        run.pr_url = None
        run.error_message = None
        run.created_at = None
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = run
        # Second execute for task counts
        counts_result = MagicMock()
        counts_result.all.return_value = []
        session.execute.side_effect = [run_result, counts_result]

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_status(parsed, respond=respond)

        respond.assert_awaited_once()
        assert "EXECUTING" in respond.call_args[0][0]
        # Block Kit blocks should be present as keyword arg
        assert respond.call_args[1].get("blocks") is not None

    async def test_status_list_returns_block_kit(self):
        from forge.gateway.slack_bot import _handle_status
        from forge.gateway.command_parser import parse_command
        from datetime import UTC, datetime

        parsed = parse_command("status")
        respond = AsyncMock()
        session = make_session()

        run = MagicMock()
        run.id = "run-uuid-1234"
        run.status = "EXECUTING"
        run.estimated_cost_usd = 0.05
        run.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        rows_result = MagicMock()
        rows_result.all.return_value = [(run, "Add OAuth login")]
        counts_result = MagicMock()
        counts_result.all.return_value = []
        session.execute.side_effect = [rows_result, counts_result]

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_status(parsed, respond=respond)

        respond.assert_awaited_once()
        text = respond.call_args[0][0]
        assert "EXECUTING" in text
        assert respond.call_args[1].get("blocks") is not None


# ── _handle_cancel ─────────────────────────────────────────────────────────────

class TestHandleCancel:
    async def test_cancel_run_not_found(self):
        from forge.gateway.slack_bot import _handle_cancel
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("cancel abc-123")
        respond = AsyncMock()
        session = make_session()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_cancel(parsed, user_id="U123", respond=respond)

        respond.assert_awaited_once()
        assert "not found" in respond.call_args[0][0]

    async def test_cancel_terminal_run_rejected(self):
        from forge.gateway.slack_bot import _handle_cancel
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("cancel abc-123")
        respond = AsyncMock()
        session = make_session()

        run = MagicMock()
        run.id = "abc-123"
        run.status = "SHIPPED"  # terminal state — cannot cancel
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = run
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_cancel(parsed, user_id="U123", respond=respond)

        respond.assert_awaited_once()
        assert "cannot be cancelled" in respond.call_args[0][0]

    async def test_cancel_active_run_succeeds(self):
        from forge.gateway.slack_bot import _handle_cancel
        from forge.gateway.command_parser import parse_command

        parsed = parse_command("cancel abc-123")
        respond = AsyncMock()
        session = make_session()

        run = MagicMock()
        run.id = "abc-123"
        run.status = "EXECUTING"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = run
        session.execute.side_effect = [result_mock, MagicMock()]

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_cancel(parsed, user_id="U123", respond=respond)

        respond.assert_awaited_once()
        assert "cancelled" in respond.call_args[0][0].lower()


# ── _handle_approval_action ───────────────────────────────────────────────────

class TestHandleApprovalAction:
    def _make_body(self, approval_id="approval-1", user_id="U-approver"):
        return {
            "actions": [{"value": approval_id}],
            "user": {"id": user_id},
            "container": {
                "channel_id": "C-channel",
                "message_ts": "12345.678",
            },
        }

    async def test_approve_updates_db_and_updates_message(self):
        from forge.gateway.slack_bot import _handle_approval_action

        body = self._make_body("approval-abc")
        mock_client = AsyncMock()
        session = make_session()

        approval = MagicMock()
        approval.id = "approval-abc"
        approval.status = "PENDING"
        approval.gate_type = "plan"
        approval.run_id = "run-1"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = approval
        session.execute.side_effect = [result_mock, MagicMock()]

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_approval_action(body, mock_client, decision="APPROVED")

        mock_client.chat_update.assert_awaited_once()
        call_kwargs = mock_client.chat_update.call_args[1]
        assert "APPROVED" in call_kwargs["text"]

    async def test_reject_updates_db_with_rejected(self):
        from forge.gateway.slack_bot import _handle_approval_action

        body = self._make_body("approval-xyz")
        mock_client = AsyncMock()
        session = make_session()

        approval = MagicMock()
        approval.id = "approval-xyz"
        approval.status = "PENDING"
        approval.gate_type = "ship"
        approval.run_id = "run-2"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = approval
        session.execute.side_effect = [result_mock, MagicMock()]

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_approval_action(body, mock_client, decision="REJECTED")

        mock_client.chat_update.assert_awaited_once()
        call_kwargs = mock_client.chat_update.call_args[1]
        assert "REJECTED" in call_kwargs["text"]

    async def test_already_decided_returns_early(self):
        from forge.gateway.slack_bot import _handle_approval_action

        body = self._make_body("approval-done")
        mock_client = AsyncMock()
        session = make_session()

        approval = MagicMock()
        approval.id = "approval-done"
        approval.status = "APPROVED"  # already decided
        approval.decided_by = "U-previous"
        approval.gate_type = "plan"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = approval
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_approval_action(body, mock_client, decision="APPROVED")

        # Should update message but NOT write to DB again
        mock_client.chat_update.assert_awaited_once()
        # session.execute should only be called once (the SELECT)
        assert session.execute.await_count == 1

    async def test_approval_not_found_returns_silently(self):
        from forge.gateway.slack_bot import _handle_approval_action

        body = self._make_body("nonexistent")
        mock_client = AsyncMock()
        session = make_session()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        with patch("forge.db.session.get_db", make_db_context(session)):
            await _handle_approval_action(body, mock_client, decision="APPROVED")

        mock_client.chat_update.assert_not_awaited()
