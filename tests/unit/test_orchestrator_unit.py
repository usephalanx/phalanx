"""
Unit tests for phalanx/workflow/orchestrator.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.workflow.orchestrator import (
    _STALE_TASK_TIMEOUT_SECONDS,
    OrchestratorError,
    WorkflowOrchestrator,
)
from phalanx.workflow.state_machine import RunStatus


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
        from phalanx.workflow.state_machine import InvalidTransitionError

        # RESEARCHING → INTAKE is an invalid non-terminal transition
        with pytest.raises(InvalidTransitionError):
            await orchestrator._transition(RunStatus.RESEARCHING, RunStatus.INTAKE)


class TestDispatchAndWait:
    def _make_poll_get_db(self, refreshed_task):
        """Build a mock get_db() context manager that returns refreshed_task on poll."""
        from contextlib import asynccontextmanager

        poll_session = AsyncMock()
        poll_result = MagicMock()
        poll_result.scalar_one.return_value = refreshed_task
        poll_session.execute = AsyncMock(return_value=poll_result)

        @asynccontextmanager
        async def _mock_get_db():
            yield poll_session

        return _mock_get_db

    async def test_completed_task_returns_normally(self, orchestrator, mock_session, mock_router):
        task = make_task("task-uuid-1", agent_role="builder", status="PENDING")
        completed_task = make_task("task-uuid-1", agent_role="builder", status="COMPLETED")

        # mock_session handles the IN_PROGRESS update; get_db() handles the poll
        mock_session.execute.return_value = MagicMock(scalar_one=MagicMock(return_value=None))

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db(completed_task)),
        ):
            await orchestrator._dispatch_and_wait(task)

        mock_router.dispatch.assert_called_once()

    async def test_failed_task_raises_orchestrator_error(
        self, orchestrator, mock_session, mock_router
    ):
        task = make_task("task-uuid-2", agent_role="qa", status="PENDING")
        failed_task = make_task(
            "task-uuid-2", agent_role="qa", status="FAILED", error="tests failed"
        )

        mock_session.execute.return_value = MagicMock(scalar_one=MagicMock(return_value=None))

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db(failed_task)),
            pytest.raises(OrchestratorError, match="failed"),
        ):
            await orchestrator._dispatch_and_wait(task)


class TestExecuteDag:
    """Tests for the DAG-aware execution path."""

    def _make_poll_get_db(self, statuses: dict[str, str]):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _mock_get_db():
            poll_session = AsyncMock()

            def _execute(stmt):
                result = MagicMock()
                # Return a task whose status depends on its ID
                task = MagicMock()
                # We can't easily know the task_id from the stmt, so always COMPLETED
                task.status = "COMPLETED"
                task.agent_role = "builder"
                task.error = None
                result.scalar_one.return_value = task
                return result

            poll_session.execute = AsyncMock(side_effect=_execute)
            yield poll_session

        return _mock_get_db

    async def test_dag_path_dispatches_ready_tasks(self, orchestrator, mock_session, mock_router):
        from phalanx.workflow.dag import DagNode, DagResolver

        t1 = make_task("t1", agent_role="builder")
        t2 = make_task("t2", agent_role="reviewer")

        nodes = {
            "t1": DagNode(task_id="t1", agent_role="builder", estimated_minutes=30),
            "t2": DagNode(
                task_id="t2", agent_role="reviewer", estimated_minutes=15, deps={"t1": "full"}
            ),
        }
        task_map = {"t1": t1, "t2": t2}

        mock_session.execute.return_value = MagicMock()

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db({})),
        ):
            await orchestrator._execute_dag(nodes, task_map, DagResolver())

        assert mock_router.dispatch.call_count == 2

    async def test_execute_uses_dag_when_flag_enabled(
        self, orchestrator, mock_session, mock_router
    ):
        """execute() routes to DAG path when phalanx_enable_dag_orchestration=True."""
        t1 = make_task("t1")
        tasks_result = MagicMock()
        tasks_result.scalars.return_value = iter([t1])

        deps_result = MagicMock()
        deps_result.scalars.return_value = iter([])

        mock_session.execute = AsyncMock(side_effect=[tasks_result, deps_result, MagicMock()])

        with (
            patch("phalanx.workflow.orchestrator.get_settings") as mock_settings,
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db({})),
            patch.object(orchestrator, "_transition", AsyncMock()),
        ):
            mock_settings.return_value.phalanx_enable_dag_orchestration = True
            await orchestrator.execute()

        mock_router.dispatch.assert_called_once()


class TestRequestShipApproval:
    async def test_ship_approval_creates_gate(self, orchestrator, mock_session):
        mock_gate = AsyncMock()
        mock_gate.request_and_wait = AsyncMock(return_value=MagicMock(status="APPROVED"))

        # request_ship_approval now loads run + tasks after approval for run_complete
        run_result = MagicMock()
        run_result.scalar_one.return_value = MagicMock()
        tasks_result = MagicMock()
        tasks_result.scalars.return_value = iter([])
        mock_session.execute = AsyncMock(side_effect=[run_result, tasks_result])

        with (
            patch("phalanx.workflow.orchestrator.ApprovalGate", return_value=mock_gate),
            patch.object(orchestrator, "_transition", AsyncMock()),
        ):
            await orchestrator.request_ship_approval(context_snapshot={"task_count": 3})

        mock_gate.request_and_wait.assert_awaited_once()

    async def test_ship_approval_rejected_raises(self, orchestrator, mock_session):
        from phalanx.workflow.approval_gate import ApprovalRejectedError

        mock_gate = AsyncMock()
        mock_gate.request_and_wait = AsyncMock(
            side_effect=ApprovalRejectedError("ship", "not ready")
        )

        with (
            patch("phalanx.workflow.orchestrator.ApprovalGate", return_value=mock_gate),
            patch.object(orchestrator, "_transition", AsyncMock()),
            pytest.raises(ApprovalRejectedError),
        ):
            await orchestrator.request_ship_approval()


class TestSlackNotifierIntegration:
    """WorkflowOrchestrator calls SlackNotifier at the right lifecycle points."""

    def _make_poll_get_db(self, refreshed_task):
        from contextlib import asynccontextmanager

        poll_session = AsyncMock()
        poll_result = MagicMock()
        poll_result.scalar_one.return_value = refreshed_task
        poll_session.execute = AsyncMock(return_value=poll_result)

        @asynccontextmanager
        async def _mock_get_db():
            yield poll_session

        return _mock_get_db

    def _make_notifier(self):
        n = MagicMock()
        n.task_started = AsyncMock()
        n.task_completed = AsyncMock()
        n.task_failed = AsyncMock()
        n.run_complete = AsyncMock()
        return n

    def _make_orch(self, mock_session, mock_router, notifier):
        return WorkflowOrchestrator(
            session=mock_session,
            run_id="run-uuid-1",
            task_router=mock_router,
            approval_timeout_hours=24,
            notifier=notifier,
        )

    async def test_task_started_called_on_dispatch(self, mock_session, mock_router):
        """_dispatch_and_wait calls notifier.task_started(task) after marking IN_PROGRESS."""
        notifier = self._make_notifier()
        orch = self._make_orch(mock_session, mock_router, notifier)

        task = make_task("t1", agent_role="builder", status="PENDING")
        completed = make_task("t1", agent_role="builder", status="COMPLETED")
        mock_session.execute.return_value = MagicMock()

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db(completed)),
        ):
            await orch._dispatch_and_wait(task)

        notifier.task_started.assert_awaited_once_with(task)

    async def test_task_completed_called_on_success(self, mock_session, mock_router):
        """_dispatch_and_wait calls notifier.task_completed(refreshed) on COMPLETED."""
        notifier = self._make_notifier()
        orch = self._make_orch(mock_session, mock_router, notifier)

        task = make_task("t1", agent_role="builder", status="PENDING")
        completed = make_task("t1", agent_role="builder", status="COMPLETED")
        mock_session.execute.return_value = MagicMock()

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db(completed)),
        ):
            await orch._dispatch_and_wait(task)

        notifier.task_completed.assert_awaited_once_with(completed)

    async def test_task_failed_called_before_raise(self, mock_session, mock_router):
        """_dispatch_and_wait calls notifier.task_failed(refreshed) before OrchestratorError."""
        notifier = self._make_notifier()
        orch = self._make_orch(mock_session, mock_router, notifier)

        task = make_task("t2", agent_role="builder", status="PENDING")
        failed = make_task("t2", agent_role="builder", status="FAILED", error="tests failed")
        mock_session.execute.return_value = MagicMock()

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db(failed)),
            pytest.raises(OrchestratorError, match="failed"),
        ):
            await orch._dispatch_and_wait(task)

        notifier.task_failed.assert_awaited_once_with(failed)

    async def test_run_complete_called_after_ship_approval(self, mock_session, mock_router):
        """request_ship_approval calls notifier.run_complete(run, tasks) after READY_TO_MERGE."""
        notifier = self._make_notifier()
        orch = self._make_orch(mock_session, mock_router, notifier)

        mock_gate = AsyncMock()
        mock_gate.request_and_wait = AsyncMock()

        mock_run = MagicMock()
        mock_tasks = [make_task("t1"), make_task("t2")]

        run_result = MagicMock()
        run_result.scalar_one.return_value = mock_run

        tasks_result = MagicMock()
        tasks_result.scalars.return_value = iter(mock_tasks)

        mock_session.execute = AsyncMock(side_effect=[run_result, tasks_result])

        with (
            patch("phalanx.workflow.orchestrator.ApprovalGate", return_value=mock_gate),
            patch.object(orch, "_transition", AsyncMock()),
        ):
            await orch.request_ship_approval(context_snapshot={"task_count": 2})

        notifier.run_complete.assert_awaited_once_with(mock_run, mock_tasks)

    async def test_notifier_noop_when_not_passed(self, mock_session, mock_router):
        """Orchestrator with no notifier= still works — default no-op never raises."""
        orch = WorkflowOrchestrator(
            session=mock_session,
            run_id="run-uuid-1",
            task_router=mock_router,
        )
        task = make_task("t1", agent_role="builder", status="PENDING")
        completed = make_task("t1", agent_role="builder", status="COMPLETED")
        mock_session.execute.return_value = MagicMock()

        with (
            patch("phalanx.workflow.orchestrator.asyncio.sleep", AsyncMock()),
            patch("phalanx.db.session.get_db", self._make_poll_get_db(completed)),
        ):
            await orch._dispatch_and_wait(task)  # must not raise


# ── Stale task watchdog ───────────────────────────────────────────────────────


def make_task_with_start(
    task_id="task-1",
    agent_role="builder",
    status="IN_PROGRESS",
    started_at=None,
    error=None,
):
    task = MagicMock()
    task.id = task_id
    task.agent_role = agent_role
    task.status = status
    task.started_at = started_at
    task.error = error
    return task


class TestStaleTaskWatchdog:
    """_poll_in_flight detects and fails tasks stuck IN_PROGRESS past the timeout."""

    def _make_poll_get_db_for_task(self, task):
        """Return a get_db() mock that yields a session returning `task`."""

        @asynccontextmanager
        async def _mock_get_db():
            poll_session = AsyncMock()
            result = MagicMock()
            result.scalar_one.return_value = task
            poll_session.execute = AsyncMock(return_value=result)
            poll_session.commit = AsyncMock()
            yield poll_session

        return _mock_get_db

    async def test_stale_in_progress_task_marked_failed(self, orchestrator):
        """A task IN_PROGRESS for > _STALE_TASK_TIMEOUT_SECONDS is marked FAILED."""
        old_start = datetime.now(UTC) - timedelta(seconds=_STALE_TASK_TIMEOUT_SECONDS + 60)
        stale_task = make_task_with_start(
            task_id="stale-task",
            agent_role="builder",
            status="IN_PROGRESS",
            started_at=old_start,
        )

        with patch("phalanx.db.session.get_db", self._make_poll_get_db_for_task(stale_task)):
            completed, failed = await orchestrator._poll_in_flight({"stale-task"})

        assert "stale-task" in failed
        assert "stale-task" not in completed

    async def test_fresh_in_progress_task_not_marked_stale(self, orchestrator):
        """A task IN_PROGRESS for less than the timeout is left alone."""
        fresh_start = datetime.now(UTC) - timedelta(seconds=60)  # 1 minute running
        fresh_task = make_task_with_start(
            task_id="fresh-task",
            agent_role="builder",
            status="IN_PROGRESS",
            started_at=fresh_start,
        )

        with patch("phalanx.db.session.get_db", self._make_poll_get_db_for_task(fresh_task)):
            completed, failed = await orchestrator._poll_in_flight({"fresh-task"})

        assert "fresh-task" not in failed
        assert "fresh-task" not in completed

    async def test_in_progress_no_start_time_not_marked_stale(self, orchestrator):
        """A task IN_PROGRESS with no started_at is not falsely detected as stale."""
        task = make_task_with_start(
            task_id="no-start-task",
            agent_role="builder",
            status="IN_PROGRESS",
            started_at=None,
        )

        with patch("phalanx.db.session.get_db", self._make_poll_get_db_for_task(task)):
            completed, failed = await orchestrator._poll_in_flight({"no-start-task"})

        assert "no-start-task" not in failed
        assert "no-start-task" not in completed

    async def test_completed_task_not_affected_by_watchdog(self, orchestrator):
        """COMPLETED tasks are returned as completed, never as failed."""
        old_start = datetime.now(UTC) - timedelta(seconds=_STALE_TASK_TIMEOUT_SECONDS + 3600)
        task = make_task_with_start(
            task_id="done-task",
            agent_role="builder",
            status="COMPLETED",
            started_at=old_start,
        )

        with patch("phalanx.db.session.get_db", self._make_poll_get_db_for_task(task)):
            completed, failed = await orchestrator._poll_in_flight({"done-task"})

        assert "done-task" in completed
        assert "done-task" not in failed

    def test_stale_timeout_constant_value(self):
        """_STALE_TASK_TIMEOUT_SECONDS must be > builder soft_time_limit (1800s)."""
        assert _STALE_TASK_TIMEOUT_SECONDS > 1800
        # And should not be excessively long (< 4h)
        assert _STALE_TASK_TIMEOUT_SECONDS < 4 * 3600
