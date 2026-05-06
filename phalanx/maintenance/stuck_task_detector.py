"""v1.7.3 runtime hardening — heartbeat-aware stuck-task detector.

Runs every 2 minutes via redbeat. For each Task in IN_PROGRESS:

  - If last_heartbeat_at is older than ttl_seconds (or per-role
    default), the task is considered worker-hung.
  - We mark the task TIMED_OUT, write a structured error, propagate
    failure_class=FAILED_INFRA_WORKER_HANG to the parent Run, and
    transition the Run to TIMED_OUT (FAILED for state-machine
    compatibility; failure_class carries the precise reason).
  - For sandbox-using agents (SRE, engineer), we trigger best-effort
    container cleanup based on the upstream sre_setup container_id.

Design rules:
  - Detector runs as its own Celery beat task; failure here MUST NOT
    cascade. Each row is processed independently.
  - The detector NEVER waits for a hung worker — it just writes the
    terminal state. The actual Celery process may continue running
    until SoftTimeLimitExceeded fires; we accept the wasted compute
    and unblock the run immediately.
  - All actions are idempotent: re-running the detector on the same
    row produces no additional writes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.db.models import Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.observability.runtime_events import (
    run_finalized,
    sandbox_cleanup as event_sandbox_cleanup,
    task_timeout,
)
from phalanx.queue.celery_app import celery_app
from phalanx.runtime.heartbeat import (
    DEFAULT_TTL_SECONDS,
    default_ttl_for_role,
    is_stale,
)
from phalanx.runtime.infra_verdicts import FAILED_INFRA_WORKER_HANG

log = structlog.get_logger(__name__)


# Sandbox-using agent roles. When a task with one of these roles times
# out, we look up the upstream sre_setup output to find the container
# and ask the provisioner to stop+remove it.
_SANDBOX_USING_ROLES = frozenset(
    {
        "cifix_sre",
        "cifix_sre_setup",
        "cifix_sre_verify",
        "cifix_engineer",
    }
)


@celery_app.task(
    name="phalanx.maintenance.stuck_task_detector.detect_stuck_tasks",
    bind=True,
    max_retries=3,
)
def detect_stuck_tasks(self) -> dict:
    """Sync entry point invoked by celery beat every 2 minutes."""
    return asyncio.run(_detect_stuck_tasks_impl())


async def _detect_stuck_tasks_impl() -> dict:
    """Scan IN_PROGRESS tasks; flag heartbeat-stale ones as TIMED_OUT.

    Returns a summary dict with counts. Idempotent — re-running on the
    same workspace produces zero additional writes.
    """
    async with get_db() as session:
        result = await session.execute(
            select(Task).where(Task.status == "IN_PROGRESS")
        )
        in_progress: list[Task] = list(result.scalars().all())

    if not in_progress:
        log.debug("runtime.stuck_detector.no_in_progress")
        return {"scanned": 0, "timed_out": 0, "ids": []}

    now = datetime.now(UTC)
    stuck: list[Task] = []
    for t in in_progress:
        if is_stale(
            last_heartbeat_at=t.last_heartbeat_at,
            ttl_seconds=t.ttl_seconds,
            role=t.agent_role,
            now=now,
        ):
            stuck.append(t)

    if not stuck:
        log.debug("runtime.stuck_detector.all_healthy", scanned=len(in_progress))
        return {"scanned": len(in_progress), "timed_out": 0, "ids": []}

    timed_out_ids: list[str] = []
    for t in stuck:
        await _time_out_task_and_propagate(t, now=now)
        timed_out_ids.append(t.id)

    log.info(
        "runtime.stuck_detector.swept",
        scanned=len(in_progress),
        timed_out=len(timed_out_ids),
        ids=timed_out_ids,
    )
    return {"scanned": len(in_progress), "timed_out": len(timed_out_ids), "ids": timed_out_ids}


async def _time_out_task_and_propagate(t: Task, *, now: datetime) -> None:
    """Mark `t` TIMED_OUT, propagate to its Run, trigger sandbox cleanup."""
    ttl = t.ttl_seconds if t.ttl_seconds is not None else default_ttl_for_role(t.agent_role)
    age_s = (
        (now - t.last_heartbeat_at).total_seconds()
        if t.last_heartbeat_at is not None
        else float("inf")
    )

    error_msg = (
        f"stuck_task_detector: heartbeat stale "
        f"({age_s:.0f}s > ttl={ttl}s) — TIMED_OUT"
    )

    try:
        async with get_db() as session:
            # 1. Mark task TIMED_OUT (idempotent — re-marking same row is fine)
            await session.execute(
                update(Task)
                .where(Task.id == t.id, Task.status == "IN_PROGRESS")
                .values(
                    status="TIMED_OUT",
                    error=error_msg,
                    completed_at=now,
                )
            )

            # 2. Look up the parent Run and propagate.
            run_result = await session.execute(
                select(Run).where(Run.id == t.run_id)
            )
            run = run_result.scalar_one_or_none()

            if run is not None and run.status not in (
                "SHIPPED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            ):
                await session.execute(
                    update(Run)
                    .where(Run.id == run.id)
                    .values(
                        status="FAILED",
                        failure_class=FAILED_INFRA_WORKER_HANG,
                        error_message=(
                            f"stuck_task_detector: task {t.id[:8]} "
                            f"({t.agent_role}) heartbeat stale "
                            f"{age_s:.0f}s > ttl={ttl}s"
                        ),
                        completed_at=now,
                    )
                )
                # Cancel any sibling tasks still waiting in the DAG.
                await session.execute(
                    update(Task)
                    .where(
                        Task.run_id == run.id,
                        Task.status.in_(["PENDING", "IN_PROGRESS"]),
                        Task.id != t.id,
                    )
                    .values(
                        status="CANCELLED",
                        error="parent run terminated by stuck-task detector",
                    )
                )

            await session.commit()
    except Exception as exc:  # noqa: BLE001 — never let detector cascade
        log.exception(
            "runtime.stuck_detector.timeout_propagation_failed",
            task_id=t.id,
            run_id=t.run_id,
            error=str(exc),
        )
        return

    task_timeout(
        task_id=t.id,
        run_id=t.run_id,
        agent_role=t.agent_role,
        age_seconds=age_s,
        ttl_seconds=ttl,
    )

    if run is not None:
        run_finalized(
            run_id=run.id,
            final_status="TIMED_OUT",
            failure_class=FAILED_INFRA_WORKER_HANG,
            reason=f"task {t.id[:8]} ({t.agent_role}) hung",
        )

    # 3. Sandbox cleanup for sandbox-using agents.
    if t.agent_role in _SANDBOX_USING_ROLES:
        await _cleanup_sandbox_for_run(t.run_id)


async def _cleanup_sandbox_for_run(run_id: str) -> None:
    """Best-effort: find the run's sandbox container and stop it."""
    try:
        from phalanx.ci_fixer_v3.provisioner import stop_sandbox  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(
                select(Task.output)
                .where(
                    Task.run_id == run_id,
                    Task.agent_role.in_(["cifix_sre", "cifix_sre_setup"]),
                    Task.status.in_(["COMPLETED", "TIMED_OUT", "FAILED"]),
                )
                .order_by(Task.sequence_num.asc())
                .limit(1)
            )
            row = result.one_or_none()

        if row is None or row[0] is None or not isinstance(row[0], dict):
            event_sandbox_cleanup(
                run_id=run_id,
                container_id=None,
                ok=False,
                reason="no_sre_setup_output_found",
            )
            return

        container_id = row[0].get("container_id")
        if not container_id:
            event_sandbox_cleanup(
                run_id=run_id,
                container_id=None,
                ok=False,
                reason="sre_setup_output_missing_container_id",
            )
            return

        await stop_sandbox(container_id)
        event_sandbox_cleanup(
            run_id=run_id,
            container_id=container_id,
            ok=True,
            reason="stuck_task_detector_terminated_run",
        )
    except Exception as exc:  # noqa: BLE001
        event_sandbox_cleanup(
            run_id=run_id,
            container_id=None,
            ok=False,
            reason="cleanup_exception",
            error=str(exc),
        )
