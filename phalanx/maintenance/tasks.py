"""Maintenance scheduled tasks.

Detects runs stuck in non-terminal states and auto-cancels them after a
hard timeout. Phase 2 of v1.6 sprint replaces the prior stub with a real
implementation. Fires every 5 minutes via redbeat (see celery_app.py).

The watchdog targets v3 runs (work_orders.work_order_type='ci_fix') in
EXECUTING or VERIFYING with `updated_at` older than
STUCK_RUN_THRESHOLD_MINUTES. Found runs and their in-flight child tasks
are flipped to FAILED with a structured error_message that the dashboard
+ Slack alert can fingerprint.

We don't try to RECOVER stuck runs — just bound them so prod doesn't
accumulate zombies. Recovery / re-dispatch is a separate problem class.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update

from phalanx.db.models import Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


STUCK_RUN_THRESHOLD_MINUTES: int = 30
"""How long a Run can sit in EXECUTING/VERIFYING with no updates before
the reaper kills it. 30 min covers worst-case agentic SRE setup +
3-iteration fix loop with comfortable margin. Bug #12 (iter-2 stuck
2026-04-28) sat for 80+ min before manual intervention; this catches it
within 30."""


@celery_app.task(
    name="phalanx.maintenance.tasks.check_blocked_runs",
    bind=True,
    max_retries=3,
)
def check_blocked_runs(self) -> dict:
    """Sync entry point invoked by celery beat every 5 minutes."""
    return asyncio.run(_check_blocked_runs_impl())


async def _check_blocked_runs_impl() -> dict:
    """Find v3 ci_fix runs stuck > STUCK_RUN_THRESHOLD_MINUTES; mark
    them and their in-flight tasks FAILED. Returns count killed."""
    cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_RUN_THRESHOLD_MINUTES)
    killed_run_ids: list[str] = []

    async with get_db() as session:
        result = await session.execute(
            select(Run)
            .join(WorkOrder, WorkOrder.id == Run.work_order_id)
            .where(
                Run.status.in_(["EXECUTING", "VERIFYING"]),
                Run.updated_at < cutoff,
                WorkOrder.work_order_type == "ci_fix",
            )
        )
        stuck_runs = result.scalars().all()

        if not stuck_runs:
            log.info("v3.reaper.no_stuck_runs", threshold_min=STUCK_RUN_THRESHOLD_MINUTES)
            return {"killed": 0}

        for run in stuck_runs:
            age_min = (datetime.now(UTC) - run.updated_at).total_seconds() / 60
            log.warning(
                "v3.reaper.killing_stuck_run",
                run_id=run.id,
                run_status=run.status,
                age_min=round(age_min, 1),
            )
            await session.execute(
                update(Run)
                .where(Run.id == run.id)
                .values(
                    status="FAILED",
                    error_message=(
                        f"reaper: stuck > {STUCK_RUN_THRESHOLD_MINUTES}min "
                        f"in state={run.status} (age={age_min:.0f}min)"
                    ),
                )
            )
            await session.execute(
                update(Task)
                .where(
                    Task.run_id == run.id,
                    Task.status.in_(["IN_PROGRESS", "PENDING"]),
                )
                .values(
                    status="FAILED",
                    error="reaper: parent run terminated",
                )
            )
            killed_run_ids.append(run.id)
        await session.commit()

    log.info(
        "v3.reaper.done",
        killed_count=len(killed_run_ids),
        killed_ids=killed_run_ids,
    )
    return {"killed": len(killed_run_ids), "ids": killed_run_ids}
