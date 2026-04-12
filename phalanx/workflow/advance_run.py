"""
advance_run — stateless, idempotent Celery task that drives a Run one step forward.

This replaces the long-running WorkflowOrchestrator loop that used to live inside
the Commander's Celery task. The old design meant every deploy (worker restart)
orphaned any in-progress run permanently. This design is deploy-safe:

    advance_run(run_id)
        1. Acquire a short Redis lock (NX EX 30) — prevents double-dispatch.
        2. Load Run — if already terminal, exit.
        3. Find the next PENDING task (sequential mode).
        4. If all tasks are COMPLETED → transition EXECUTING → VERIFYING.
        5. If an IN_PROGRESS task exists, check if it is stale → reset to PENDING.
        6. Otherwise dispatch the next PENDING task and schedule itself to re-check
           in _POLL_INTERVAL seconds (countdown).

Reliability guarantees:
    - Idempotent: re-running the same run_id at the same state is a no-op.
    - Crash-safe: the watchdog in maintenance/tasks.py re-calls advance_run for
      any EXECUTING run that has no IN_PROGRESS task and no active advance lock.
    - No event-loop binding: each call is a fresh asyncio.run() in a fresh
      Celery task — no session re-use across sleeps.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import redis as _redis_lib
import structlog
from sqlalchemy import select, update

from phalanx.config.settings import get_settings
from phalanx.db.models import Run, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app
from phalanx.runtime.task_router import TaskRouter
from phalanx.workflow.state_machine import RunStatus, validate_transition

log = structlog.get_logger(__name__)

# Seconds between re-checks while waiting for an in-flight agent task to finish.
_POLL_INTERVAL = 15

# If a task stays IN_PROGRESS longer than this, it is presumed dead (OOM /
# SIGKILL) and reset to PENDING so the next advance_run re-dispatches it.
_STALE_TASK_TIMEOUT_SECONDS = 2700  # 45 min — same as orchestrator watchdog

# Max countdown between advance_run re-checks (caps exponential back-off).
_MAX_COUNTDOWN = 60

# Redis key pattern for the advance lock.
_LOCK_KEY = "advance:{run_id}"
_LOCK_TTL = 30  # seconds — must be longer than the DB + Celery dispatch round-trip

# Terminal states — advance_run exits immediately for these.
_TERMINAL_STATES = frozenset({"FAILED", "CANCELLED", "SHIPPED", "MERGED", "READY_TO_MERGE"})


# ── Celery task ───────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.workflow.advance_run.advance_run",
    bind=True,
    queue="commander",
    max_retries=5,
    acks_late=True,
    soft_time_limit=120,
    time_limit=180,
)
def advance_run(self, run_id: str, attempt: int = 0) -> dict:  # pragma: no cover
    """
    Celery task: drive Run ``run_id`` one step forward.

    Called by:
        - CommanderAgent after plan approval (replaces ``await orch.execute()``)
        - Maintenance watchdog for orphaned EXECUTING runs
        - Itself (scheduled re-check via countdown)
    """
    return asyncio.run(_advance_run_async(run_id, attempt))


# ── Async implementation ──────────────────────────────────────────────────────


async def _advance_run_async(run_id: str, attempt: int) -> dict:
    """Core logic — separated so it can be called from tests without Celery."""
    logger = log.bind(run_id=run_id, attempt=attempt)

    # ── 1. Acquire idempotency lock ───────────────────────────────────────────
    settings = get_settings()
    redis_url = settings.redis_url

    redis_client = _redis_lib.from_url(redis_url, decode_responses=True)
    lock_key = _LOCK_KEY.format(run_id=run_id)
    acquired = redis_client.set(lock_key, "1", nx=True, ex=_LOCK_TTL)

    if not acquired:
        logger.info("advance_run.lock_busy")
        return {"status": "lock_busy", "run_id": run_id}

    try:
        return await _step(run_id, logger, redis_client)
    finally:
        redis_client.delete(lock_key)


async def _step(run_id: str, logger, redis_client) -> dict:
    """Single orchestration step inside the lock."""
    async with get_db() as session:
        # ── 2. Load Run ───────────────────────────────────────────────────────
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()

        if run is None:
            logger.error("advance_run.run_not_found")
            return {"status": "not_found", "run_id": run_id}

        if run.status in _TERMINAL_STATES:
            logger.info("advance_run.terminal", status=run.status)
            return {"status": "terminal", "run_status": run.status}

        if run.status not in ("EXECUTING", "VERIFYING"):
            logger.info("advance_run.not_executing", status=run.status)
            return {"status": "not_executing", "run_status": run.status}

        # ── 3. Load tasks ─────────────────────────────────────────────────────
        tasks_result = await session.execute(
            select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num)
        )
        tasks = list(tasks_result.scalars())

        if not tasks:
            logger.error("advance_run.no_tasks")
            return {"status": "no_tasks", "run_id": run_id}

        # ── 4. Check for stale IN_PROGRESS tasks ─────────────────────────────
        now = datetime.now(UTC)
        stale_reset = 0
        for task in tasks:
            if task.status == "IN_PROGRESS" and task.started_at:
                elapsed = (now - task.started_at).total_seconds()
                if elapsed > _STALE_TASK_TIMEOUT_SECONDS:
                    logger.warning(
                        "advance_run.stale_task_reset",
                        task_id=task.id,
                        agent_role=task.agent_role,
                        elapsed_s=int(elapsed),
                    )
                    await session.execute(
                        update(Task)
                        .where(Task.id == task.id, Task.status == "IN_PROGRESS")
                        .values(
                            status="PENDING",
                            started_at=None,
                            error=f"Reset after stale timeout ({elapsed:.0f}s)",
                        )
                    )
                    stale_reset += 1
        if stale_reset:
            await session.commit()
            # Reload tasks after reset
            tasks_result = await session.execute(
                select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num)
            )
            tasks = list(tasks_result.scalars())

        # ── 5. Evaluate run state ─────────────────────────────────────────────

        # Something already in-flight and healthy — just reschedule poll
        in_progress = [t for t in tasks if t.status == "IN_PROGRESS"]
        if in_progress:
            logger.info(
                "advance_run.waiting",
                in_progress=[t.id for t in in_progress],
            )
            _schedule_recheck(run_id, countdown=_POLL_INTERVAL)
            return {"status": "waiting", "in_progress": [t.id for t in in_progress]}

        # Notify completed tasks (those that finished since last poll)
        completed_tasks = [t for t in tasks if t.status == "COMPLETED"]
        if completed_tasks:
            try:
                from phalanx.workflow.slack_notifier import SlackNotifier  # noqa: PLC0415

                notifier = await SlackNotifier.from_run(run_id, session)
                for ct in completed_tasks:
                    await notifier.task_completed(ct)
            except Exception:
                pass

        # Any FAILED task → fail the run
        failed_tasks = [t for t in tasks if t.status in ("FAILED", "CANCELLED")]
        if failed_tasks:
            ft = failed_tasks[0]
            error_msg = f"Task {ft.id} ({ft.agent_role}) failed: {ft.error or 'no detail'}"
            logger.error("advance_run.task_failed", task_id=ft.id, agent_role=ft.agent_role)
            try:
                from phalanx.workflow.slack_notifier import SlackNotifier  # noqa: PLC0415

                notifier = await SlackNotifier.from_run(run_id, session)
                await notifier.task_failed(ft)
            except Exception:
                pass
            await _transition(session, run_id, run.status, "FAILED", error_msg)
            return {"status": "run_failed", "task_id": ft.id, "error": error_msg}

        # All tasks completed → advance run state
        pending_tasks = [t for t in tasks if t.status == "PENDING"]

        if not pending_tasks and len(completed_tasks) == len(tasks):
            # All done — notify the last completed task then transition
            if run.status == "EXECUTING":
                logger.info("advance_run.all_tasks_complete")
                try:
                    from phalanx.workflow.slack_notifier import SlackNotifier  # noqa: PLC0415

                    notifier = await SlackNotifier.from_run(run_id, session)
                    await notifier.task_completed(completed_tasks[-1])
                except Exception:
                    pass
                await _transition(session, run_id, "EXECUTING", "VERIFYING")
                # The ship-approval gate is handled by CommanderAgent which was
                # waiting for us. Signal it by dispatching the verifying advance.
                _schedule_recheck(run_id, countdown=2)
            elif run.status == "VERIFYING":
                # Verifying is handled by CommanderAgent's approval gate — just log
                logger.info("advance_run.verifying_in_progress")
            return {"status": "all_complete", "run_status": run.status}

        # ── 6. Dispatch next PENDING task ────────────────────────────────────
        next_task = pending_tasks[0]  # already ordered by sequence_num

        logger.info(
            "advance_run.dispatching",
            task_id=next_task.id,
            agent_role=next_task.agent_role,
            sequence_num=next_task.sequence_num,
        )

        # Mark IN_PROGRESS before dispatch (same pattern as old orchestrator)
        await session.execute(
            update(Task).where(Task.id == next_task.id).values(status="IN_PROGRESS", started_at=now)
        )
        await session.commit()

        # Slack: task started
        try:
            from phalanx.workflow.slack_notifier import SlackNotifier  # noqa: PLC0415

            notifier = await SlackNotifier.from_run(run_id, session)
            await notifier.task_started(next_task)
        except Exception:
            pass

    # Dispatch outside the session context (avoids keeping connection open)
    router = TaskRouter(celery_app)
    router.dispatch(
        agent_role=next_task.agent_role,
        task_id=next_task.id,
        run_id=run_id,
        payload={"assigned_agent_id": next_task.assigned_agent_id},
    )

    # Schedule next poll to check if the task finished
    _schedule_recheck(run_id, countdown=_POLL_INTERVAL)

    return {
        "status": "dispatched",
        "task_id": next_task.id,
        "agent_role": next_task.agent_role,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _transition(
    session, run_id: str, from_status: str, to_status: str, error: str | None = None
) -> None:
    """Validate + apply a Run status transition."""
    validate_transition(RunStatus(from_status), RunStatus(to_status))
    values: dict = {"status": to_status, "updated_at": datetime.now(UTC)}
    if error:
        values["error_message"] = error
    await session.execute(update(Run).where(Run.id == run_id).values(**values))
    await session.commit()
    log.info("advance_run.transition", run_id=run_id, from_=from_status, to=to_status)


def _schedule_recheck(run_id: str, countdown: int = _POLL_INTERVAL) -> None:
    """Schedule the next advance_run call via Celery countdown."""
    celery_app.send_task(
        "phalanx.workflow.advance_run.advance_run",
        kwargs={"run_id": run_id},
        queue="commander",
        countdown=countdown,
    )
