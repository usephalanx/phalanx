"""Shared Task-status write for v3 agents.

Build-flow agents each inline their own `UPDATE tasks SET status='COMPLETED'`
at the end of execute() (see e.g. builder.py lines 205-216). v3 agents
share a common shape (Celery wrapper → agent.execute() → AgentResult),
so the completion write is factored here to avoid four copies.

This is ONLY called from the v3 Celery execute_task wrappers — never
from build flow, never from v2. Touching BaseAgent would have been
cleaner but risks the "don't break" constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import update

from phalanx.db.models import Task
from phalanx.db.session import get_db

if TYPE_CHECKING:
    from phalanx.agents.base import AgentResult

log = structlog.get_logger(__name__)


async def persist_task_completion(task_id: str, result: AgentResult) -> None:
    """Write the terminal Task row after a v3 agent returns.

    - success=True → status='COMPLETED', output=result.output
    - success=False → status='FAILED', error=result.error (truncated to 2000)
    Always sets completed_at. Idempotent: if the row is already in a
    terminal state (shouldn't happen, but be defensive) we still run the
    update; the status value we write is authoritative for THIS attempt.
    """
    new_status = "COMPLETED" if result.success else "FAILED"
    err_text = (result.error or "")[:2000] if not result.success else None
    try:
        async with get_db() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=new_status,
                    output=result.output,
                    error=err_text,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
    except Exception as exc:
        # Never let persistence errors propagate out of the Celery task —
        # log loudly; advance_run's stale-IN_PROGRESS timeout will eventually
        # reset the task and retry.
        log.exception(
            "v3.task_lifecycle.persist_failed",
            task_id=task_id,
            success=result.success,
            error=str(exc),
        )
