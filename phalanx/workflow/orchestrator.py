"""
Workflow Orchestrator — coordinates task execution within a Run.

Design (evidence in EXECUTION_PLAN.md §B):
  - One orchestrator instance per Run. Called by Commander.
  - Reads task plan from Postgres, dispatches tasks in dependency order.
  - Monitors task status, handles BLOCKED/ESCALATING states.
  - Inserts approval gates between workflow phases as defined in workflow.yaml.
  - AP-004: Never writes Run.status directly — always via state machine.
  - AP-001: No task-to-task direct calls; all routing via Celery queues.

Two dispatch paths (selected by feature flag):

  Legacy (phalanx_enable_dag_orchestration=False):
    Tasks are dispatched one-by-one in sequence_num order. This is the
    existing behaviour — unchanged for safety.

  DAG (phalanx_enable_dag_orchestration=True):
    TaskDependency rows are loaded, DagResolver.get_ready() is called each
    poll cycle, and all unblocked tasks are dispatched in parallel to Celery.
    Completed tasks unlock their downstream neighbours in subsequent cycles.
    A failed task halts the run immediately.

Anti-patterns it prevents:
  - Parallel task dispatch without dependency checks.
  - Agents calling each other's HTTP endpoints.
  - Status updates that bypass the state machine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from phalanx.config.settings import get_settings
from phalanx.db.models import Run, Task, TaskDependency
from phalanx.workflow.approval_gate import ApprovalGate
from phalanx.workflow.dag import DagNode, DagResolver
from phalanx.workflow.slack_notifier import SlackNotifier
from phalanx.workflow.state_machine import RunStatus, validate_transition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phalanx.runtime.task_router import TaskRouter

log = structlog.get_logger(__name__)

_TASK_POLL_INTERVAL = 15  # seconds between task status checks
_TASK_MAX_WAIT = 7200  # 2h hard cap per task (overridden by guardrails)
_COMPLETED_STATUSES = frozenset({"COMPLETED"})
_FAILED_STATUSES = frozenset({"FAILED", "CANCELLED"})
_BLOCKED_STATUSES = frozenset({"BLOCKED", "ESCALATING", "NEEDS_CLARIFICATION"})

# Safety net: mark a task FAILED if it stays IN_PROGRESS beyond this many
# seconds. This catches tasks whose Celery worker was SIGKILL'd (OOM, node
# restart) where SoftTimeLimitExceeded never fires.
# Value: builder soft_time_limit=1800s + 15min grace = 2700s.
_STALE_TASK_TIMEOUT_SECONDS = 2700


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
        notifier: SlackNotifier | None = None,
    ) -> None:
        self._session = session
        self.run_id = run_id
        self._router = task_router
        self._approval_timeout = approval_timeout_hours * 3600
        self._log = log.bind(run_id=run_id)
        self._notifier = notifier if notifier is not None else SlackNotifier(
            channel_id=None, thread_ts=None, slack_token="", enabled=False
        )

    async def execute(self) -> None:
        """
        Drive the run through EXECUTING → VERIFYING → AWAITING_SHIP_APPROVAL.

        Routes to the DAG-aware path when phalanx_enable_dag_orchestration=True,
        otherwise falls back to the legacy sequential path (unchanged behaviour).
        """
        self._log.info("orchestrator.start")

        tasks = await self._load_tasks()
        if not tasks:
            raise OrchestratorError(f"Run {self.run_id} has no tasks to execute")

        # Post live progress board — updates in-place as tasks progress
        await self._notifier.post_progress_board(tasks)

        settings = get_settings()
        if settings.phalanx_enable_dag_orchestration:
            self._log.info("orchestrator.mode", mode="dag")
            deps = await self._load_dependencies()
            resolver = DagResolver()
            nodes = resolver.build_nodes(tasks, deps)
            await self._execute_dag(nodes, {t.id: t for t in tasks}, resolver)
        else:
            self._log.info("orchestrator.mode", mode="sequential")
            for task in tasks:
                await self._dispatch_and_wait(task)

        # All tasks done — transition to VERIFYING (QA + Security run as tasks)
        self._log.info("orchestrator.all_tasks_complete")
        await self._transition(RunStatus.EXECUTING, RunStatus.VERIFYING)

    # ─────────────────────────────────────────────────────────────────────────
    # DAG-aware dispatch path
    # ─────────────────────────────────────────────────────────────────────────

    async def _execute_dag(
        self,
        nodes: dict[str, DagNode],
        task_map: dict[str, Task],
        resolver: DagResolver,
    ) -> None:
        """
        Dispatch tasks respecting the DAG.

        Algorithm (Kahn-style dispatch):
          1. Get all ready tasks (no unsatisfied deps, not already dispatched).
          2. Dispatch each to its agent queue (fire-and-forget to Celery).
          3. Sleep one poll interval, then check every in-flight task.
          4. Move completed tasks to completed_ids; fail fast on any failure.
          5. Repeat until all tasks are completed.
        """
        completed_ids: set[str] = set()
        in_flight: set[str] = set()   # dispatched, not yet COMPLETED

        while len(completed_ids) < len(nodes):
            # ── Dispatch all newly unblocked tasks ────────────────────────────
            ready = resolver.get_ready(nodes, completed_ids)
            to_dispatch = [tid for tid in ready if tid not in in_flight]

            for tid in to_dispatch:
                await self._dispatch_task(task_map[tid])
                in_flight.add(tid)
                self._log.info(
                    "orchestrator.dag.dispatched",
                    task_id=tid,
                    agent_role=nodes[tid].agent_role,
                    in_flight=len(in_flight),
                )

            # ── Safety: deadlock if nothing is running and nothing is ready ──
            if not in_flight:
                remaining = set(nodes) - completed_ids
                raise OrchestratorError(
                    f"DAG deadlock: {len(remaining)} task(s) unreachable "
                    f"(possible unsatisfied dependency). tasks={remaining}"
                )

            # ── Poll all in-flight tasks ───────────────────────────────────
            await asyncio.sleep(_TASK_POLL_INTERVAL)
            newly_done, newly_failed = await self._poll_in_flight(in_flight)

            # ── Fail fast on critical failures; tolerate qa/reviewer ──────
            if newly_failed:
                fatal = []
                fatal_ids = []
                non_fatal = []
                for fid in newly_failed:
                    t = task_map.get(fid)
                    role = t.agent_role if t else "?"
                    detail = f"{fid} ({role}): {t.error if t else 'no detail'}"
                    if role in ("qa", "reviewer", "verifier", "integration_wiring"):
                        non_fatal.append(detail)
                        self._log.warning(
                            "orchestrator.dag.non_fatal_failure",
                            task_id=fid, role=role,
                        )
                        await self._notifier.task_failed(task_map[fid])
                        # Treat as done so DAG can continue
                        newly_done.add(fid)
                    else:
                        fatal.append(detail)
                        fatal_ids.append(fid)
                newly_failed -= {fid for fid in newly_failed
                                  if (task_map.get(fid) and
                                      task_map[fid].agent_role in ("qa", "reviewer", "verifier", "integration_wiring"))}
                if fatal:
                    for fid in fatal_ids:
                        t = task_map.get(fid)
                        if t is not None:
                            await self._notifier.task_failed(t)
                    raise OrchestratorError(
                        f"DAG task(s) failed: {'; '.join(fatal)}"
                    )

            if newly_done:
                self._log.info(
                    "orchestrator.dag.batch_complete",
                    completed=sorted(newly_done),
                    total_done=len(completed_ids) + len(newly_done),
                    total=len(nodes),
                )
                for tid in newly_done - completed_ids:
                    await self._notifier.task_completed(task_map[tid])

            completed_ids |= newly_done
            in_flight -= newly_done

    async def _dispatch_task(self, task: Task) -> None:
        """Mark a task IN_PROGRESS and send it to its Celery queue."""
        await self._session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(status="IN_PROGRESS", started_at=datetime.now(UTC))
        )
        await self._session.commit()
        await self._notifier.task_started(task)

        self._router.dispatch(
            agent_role=task.agent_role,
            task_id=task.id,
            run_id=self.run_id,
            payload={"assigned_agent_id": task.assigned_agent_id},
        )

    async def _poll_in_flight(
        self, task_ids: set[str]
    ) -> tuple[set[str], set[str]]:
        """
        Poll all in-flight tasks in a single fresh DB session.

        Returns:
            (completed_ids, failed_ids) — both are subsets of task_ids.

        Side effect: tasks that have been IN_PROGRESS beyond
        _STALE_TASK_TIMEOUT_SECONDS are marked FAILED in DB (stale-task
        watchdog — catches workers killed by SIGKILL/OOM where the Celery
        soft_time_limit handler never fires).
        """
        from phalanx.db.session import get_db  # noqa: PLC0415

        completed: set[str] = set()
        failed: set[str] = set()
        now = datetime.now(UTC)

        async with get_db() as poll_session:
            for tid in task_ids:
                result = await poll_session.execute(select(Task).where(Task.id == tid))
                refreshed = result.scalar_one()

                if refreshed.status in _COMPLETED_STATUSES:
                    completed.add(tid)
                elif refreshed.status in _FAILED_STATUSES:
                    failed.add(tid)
                elif refreshed.status in _BLOCKED_STATUSES:
                    self._log.warning(
                        "orchestrator.dag.task_blocked",
                        task_id=tid,
                        status=refreshed.status,
                    )
                elif refreshed.status == "IN_PROGRESS" and refreshed.started_at:
                    # Stale-task watchdog: task has been running too long
                    elapsed = (now - refreshed.started_at).total_seconds()
                    if elapsed > _STALE_TASK_TIMEOUT_SECONDS:
                        stale_error = (
                            f"Task exceeded stale timeout "
                            f"({elapsed:.0f}s > {_STALE_TASK_TIMEOUT_SECONDS}s). "
                            f"Worker likely killed (OOM/SIGKILL) without updating DB."
                        )
                        self._log.error(
                            "orchestrator.stale_task_detected",
                            task_id=tid,
                            agent_role=refreshed.agent_role,
                            elapsed_seconds=int(elapsed),
                        )
                        await poll_session.execute(
                            update(Task)
                            .where(Task.id == tid, Task.status == "IN_PROGRESS")
                            .values(
                                status="FAILED",
                                error=stale_error,
                                failure_count=Task.failure_count + 1,
                                completed_at=now,
                            )
                        )
                        await poll_session.commit()
                        failed.add(tid)

        return completed, failed

    async def _load_dependencies(self) -> list[TaskDependency]:
        """Load all TaskDependency rows for this run's tasks."""
        result = await self._session.execute(
            select(TaskDependency)
            .join(Task, TaskDependency.task_id == Task.id)
            .where(Task.run_id == self.run_id)
        )
        return list(result.scalars())

    # ─────────────────────────────────────────────────────────────────────────
    # Legacy sequential dispatch path (unchanged)
    # ─────────────────────────────────────────────────────────────────────────

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
        await self._notifier.task_started(task)

        # Dispatch to agent queue
        self._router.dispatch(
            agent_role=task.agent_role,
            task_id=task.id,
            run_id=self.run_id,
            payload={"assigned_agent_id": task.assigned_agent_id},
        )

        # Poll for completion — open a fresh session each iteration to avoid
        # asyncpg greenlet context loss across asyncio.sleep() yields
        from phalanx.db.session import get_db  # noqa: PLC0415

        elapsed = 0
        while elapsed < _TASK_MAX_WAIT:
            await asyncio.sleep(_TASK_POLL_INTERVAL)
            elapsed += _TASK_POLL_INTERVAL

            async with get_db() as poll_session:
                result = await poll_session.execute(select(Task).where(Task.id == task.id))
                refreshed = result.scalar_one()

            if refreshed.status in _COMPLETED_STATUSES:
                self._log.info(
                    "orchestrator.task_complete",
                    task_id=task.id,
                    elapsed_s=elapsed,
                )
                await self._notifier.task_completed(refreshed)
                return

            if refreshed.status in _FAILED_STATUSES:
                await self._notifier.task_failed(refreshed)
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

        raise OrchestratorError(f"Task {task.id} ({task.agent_role}) timed out after {elapsed}s")

    # ─────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ─────────────────────────────────────────────────────────────────────────

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

        # Post final summary to the Slack thread — load fresh run + tasks
        run_result = await self._session.execute(select(Run).where(Run.id == self.run_id))
        run = run_result.scalar_one()
        tasks_result = await self._session.execute(select(Task).where(Task.run_id == self.run_id))
        tasks = list(tasks_result.scalars())
        await self._notifier.run_complete(run, tasks)
