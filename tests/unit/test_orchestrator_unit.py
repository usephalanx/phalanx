"""
Unit tests for forge/workflow/orchestrator.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.orchestrator import OrchestratorError, WorkflowOrchestrator
from forge.workflow.state_machine import RunStatus


def make_task(
    task_id="task-1",
    agent_role="builder",
    sequence_num=1,
    status="PENDING",
    assigned_agent_id=None,
    error=None,
):
    task = MagicMock()
    task.id = task_id
    task.agent_role = agent_role
    task.sequence_num = sequence_num
    task.status = status
    task.assigned_agent_id = assigned_agent_id
    task.error = error
    return task


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.expire_all = MagicMock()
    return session


@pytest.fixture
def mock_router():
    router = MagicMock()
    router.dispatch = MagicMock(return_value="celery-id-123")
    return router


@pytest.fixture
def orchestrator(mock_session, mock_router):
    return WorkflowOrchestrator(
        session=mock_session,
        run_id="run-uuid-1",
        task_router=mock_router,
        approval_timeout_hours=24,
    )


class TestLoadTasks:
    async def test_load_tasks_returns_ordered_tasks(self, orchestrator, mock_session):
        t1 = make_task("t1", sequence_num=1)
        t2 = make_task("t2", sequence_num=2)

        scalars_mock = MagicMock()
        scalars_mock.scalars.return_value = iter([t1, t2])
        mock_session.execute.return_value = scalars_mock

        tasks = await orchestrator._load_tasks()
        assert len(tasks) == 2

    async def test_empty_task_list_raises_in_execute(self, orchestrator, mock_session):
        scalars_mock = MagicMock()
        scalars_mock.scalars.return_value = iter([])
        mock_session.execute.return_value = scalars_mock

        with pytest.raises(OrchestratorError, match="no tasks"):
            await orchestrator.execute()


class TestTransition:
    async def test_valid_transition_updates_run(self, orchestrator, mock_session):
        await orchestrator._transition(RunStatus.EXECUTING, RunStatus.VERIFYING)
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    async def test_invalid_transition_raises(self, orchestrator):
        from forge.workflow.state_machine import InvalidTransitionError

        # RESEARCHING → INTAKE is an invalid non-terminal transition
        with pytest.raises(InvalidTransitionError):
            await orchestrator._transition(RunStatus.RESEARCHING, RunStatus.INTAKE)


class TestDispatchAndWait:
    async def test_completed_task_returns_normally(self, orchestrator, mock_session, mock_router):
        task = make_task("t1", agent_role="builder", status="PENDING")
        completed_task = make_task("t1", agent_role="builder", status="COMPLETED")

        execute_results = [
            MagicMock(scalar_one=MagicMock(return_value=None)),  # mark IN_PROGRESS
            MagicMock(scalar_one=MagicMock(return_value=completed_task)),  # poll
        ]
        mock_session.execute.side_effect = execute_results

        with patch("forge.workflow.orchestrator.asyncio.sleep", AsyncMock()):
            await orchestrator._dispatch_and_wait(task)

        mock_router.dispatch.assert_called_once()

    async def test_failed_task_raises_orchestrator_error(
        self, orchestrator, mock_session, mock_router
    ):
        task = make_task("t1", agent_role="qa", status="PENDING")
        failed_task = make_task("t1", agent_role="qa", status="FAILED", error="tests failed")

        execute_results = [
            MagicMock(scalar_one=MagicMock(return_value=None)),  # mark IN_PROGRESS
            MagicMock(scalar_one=MagicMock(return_value=failed_task)),  # poll
        ]
        mock_session.execute.side_effect = execute_results

        with (
            patch("forge.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            pytest.raises(OrchestratorError, match="failed"),
        ):
            await orchestrator._dispatch_and_wait(task)


class TestRequestShipApproval:
    async def test_ship_approval_creates_gate(self, orchestrator, mock_session):
        mock_gate = AsyncMock()
        mock_gate.request_and_wait = AsyncMock(return_value=MagicMock(status="APPROVED"))

        with (
            patch("forge.workflow.orchestrator.ApprovalGate", return_value=mock_gate),
            patch.object(orchestrator, "_transition", AsyncMock()),
        ):
            await orchestrator.request_ship_approval(context_snapshot={"task_count": 3})

        mock_gate.request_and_wait.assert_awaited_once()

    async def test_ship_approval_rejected_raises(self, orchestrator, mock_session):
        from forge.workflow.approval_gate import ApprovalRejectedError

        mock_gate = AsyncMock()
        mock_gate.request_and_wait = AsyncMock(
            side_effect=ApprovalRejectedError("ship", "not ready")
        )

        with (
            patch("forge.workflow.orchestrator.ApprovalGate", return_value=mock_gate),
            patch.object(orchestrator, "_transition", AsyncMock()),
            pytest.raises(ApprovalRejectedError),
        ):
            await orchestrator.request_ship_approval()
