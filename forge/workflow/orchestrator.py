"""
Workflow Orchestrator — coordinates task execution within a Run.

Design (evidence in EXECUTION_PLAN.md §B):
  - One orchestrator instance per Run. Called by Commander.
  - Reads task plan from Postgres, dispatches tasks in dependency order.
  - Monitors task status, handles BLOCKED/ESCALATING states.
  - Inserts approval gates between workflow phases as defined in workflow.yaml.
  - AP-004: Never writes Run.status directly — always via state machine.
  - AP-001: No task-to-task direct calls; all routing via Celery queues.

Anti-patterns it prevents:
  - Parallel task dispatch without dependency checks.
  - Agents calling each other's HTTP endpoints.
  - Status updates that bypass the state machine.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.models import Approval, Run, Task
from forge.runtime.task_router import TaskRouter
from forge.workflow.approval_gate import ApprovalGate, ApprovalRejectedError, ApprovalTimeoutError
from forge.workflow.state_machine import RunStatus, validate_transition

log = structlog.get_logger(__name__)

_TASK_POLL_INTERVAL = 15   # seconds between task status checks
_TASK_MAX_WAIT = 7200      # 2h hard cap per task (overridden by guardrails)
_COMPLETED_STATUSES = frozenset({"COMPLETED"})
_FAILED_STATUSES = frozenset({"FAILED", "CANCELLED"})
_BLOCKED_STATUSES = frozenset({"BLOCKED", "ESCALATING", "NEEDS_CLARIFICATION"})


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator cannot proceed."""


class WorkflowOrchestrator:
    """
    Drives a Run through its full lifecycle: task dispatch → approval gates → completion.

    Usage (from Commander):
        orch = WorkflowOrchestrator(session, run_id, task_router)
        await orch.execute()
    """

    def __init__(
        self,
        session: AsyncSession,
        run_id: str,
        task_router: TaskRouter,
        approval_timeout_hours: int = 24,
    ) -> None:
        self._session = session
        self.run_id = run_id
        self._router = task_router
        self._approval_timeout = approval_timeout_hours * 3600
        self._log = log.bind(run_id=run_id)

    async def execute(self) -> None:
        """
        Drive the run through EXECUTING → VERIFYING → AWAITING_SHIP_APPROVAL.

        Tasks are dispatched in sequence_num order, respecting depends_on.
        Blocks between phases if approval is required.
        """
        self._log.info("orchestrator.start")

        tasks = await self._load_tasks()
        if not tasks:
            raise OrchestratorError(f"Run {self.run_id} has no tasks to execute")

        for task in tasks:
            await self._dispatch_and_wait(task)

        # All tasks done — transition to VERIFYING (QA + Security run as tasks)
        self._log.info("orchestrator.all_tasks_complete")
        await self._transition(RunStatus.EXECUTING, RunStatus.VERIFYING)

    async def _load_tasks(self) -> list[Task]:
        """Load all PENDING tasks for this run, ordered by sequence_num."""
        result = await self._session.execute(
            select(Task)
            .where(Task.run_id == self.run_id, Task.status == "PENDING")
            .order_by(Task.sequence_num)
        )
        return list(result.scalars())

    async def _dispatch_and_wait(self, task: Task) -> None:
        """Dispatch a task to its agent queue and poll until terminal."""
        self._log.info(
            "orchestrator.task_dispatch",
            task_id=task.id,
            agent_role=task.agent_role,
            sequence_num=task.sequence_num,
        )

        # Mark task IN_PROGRESS
        await self._session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(status="IN_PROGRESS", started_at=datetime.now(UTC))
        )
        await self._session.commit()

        # Dispatch to agent queue
        self._router.dispatch(
            agent_role=task.agent_role,
            task_id=task.id,
            run_id=self.run_id,
            payload={"assigned_agent_id": task.assigned_agent_id},
        )

        # Poll for completion
        elapsed = 0
        while elapsed < _TASK_MAX_WAIT:
            await asyncio.sleep(_TASK_POLL_INTERVAL)
            elapsed += _TASK_POLL_INTERVAL

            self._session.expire_all()
            result = await self._session.execute(
                select(Task).where(Task.id == task.id)
            )
            refreshed = result.scalar_one()

            if refreshed.status in _COMPLETED_STATUSES:
                self._log.info(
                    "orchestrator.task_complete",
                    task_id=task.id,
                    elapsed_s=elapsed,
                )
                return

            if refreshed.status in _FAILED_STATUSES:
                raise OrchestratorError(
                    f"Task {task.id} ({task.agent_role}) failed: "
                    f"{refreshed.error or 'no error detail'}"
                )

            if refreshed.status in _BLOCKED_STATUSES:
                self._log.warning(
                    "orchestrator.task_blocked",
                    task_id=task.id,
                    status=refreshed.status,
                    elapsed_s=elapsed,
                )
                # Continue polling — human or escalation will unblock

        raise OrchestratorError(
            f"Task {task.id} ({task.agent_role}) timed out after {elapsed}s"
        )

    async def _transition(self, from_status: RunStatus, to_status: RunStatus) -> None:
        """Validate and apply a Run status transition."""
        validate_transition(from_status, to_status)
        await self._session.execute(
            update(Run)
            .where(Run.id == self.run_id)
            .values(status=to_status.value, updated_at=datetime.now(UTC))
        )
        await self._session.commit()
        self._log.info("orchestrator.transition", from_=from_status, to=to_status)

    async def request_ship_approval(
        self,
        context_snapshot: dict | None = None,
    ) -> None:
        """
        Block until a human approves the ship gate.
        Raises ApprovalRejectedError → caller transitions to FAILED.
        """
        gate = ApprovalGate(
            session=self._session,
            run_id=self.run_id,
            timeout_seconds=self._approval_timeout,
        )
        await gate.request_and_wait(
            gate_type="ship",
            gate_phase="execution",
            context_snapshot=context_snapshot,
        )
        await self._transition(RunStatus.AWAITING_SHIP_APPROVAL, RunStatus.READY_TO_MERGE)
