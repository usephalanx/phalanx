"""Celery task wrapper for the CI Fixer v2 run bootstrap.

Webhook handler dispatches here (via `execute_v2_task.apply_async`) when
`settings.phalanx_ci_fixer_v2_enabled` is True. The task is a thin shim
that spins up an asyncio loop and calls `run_bootstrap.execute_v2_run`.

Design rules:
  - No agent logic here. The task never touches AgentContext or tools;
    everything runs inside `execute_v2_run`.
  - Exceptions propagate to Celery's retry machinery (task_acks_late +
    task_reject_on_worker_lost already enforce at-least-once semantics
    per celery_app.py). A run that crashes before DB write will be
    re-dispatched when the worker lease expires.
  - Queue: `ci_fixer`. Physically scoped to `phalanx-ci-fixer-worker`
    per audit N1 (Docker socket only mounted on that service).
"""

from __future__ import annotations

import asyncio

import structlog

from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


async def _execute_v2_task_async(ci_fix_run_id: str) -> dict:
    """Inner async implementation — unit-testable without Celery wrapping.

    Returns the plain-dict summary that the Celery task also returns.
    Exceptions propagate; the Celery task wrapper logs + re-raises.
    """
    from phalanx.ci_fixer_v2.run_bootstrap import execute_v2_run

    outcome = await execute_v2_run(ci_fix_run_id)
    return {
        "ci_fix_run_id": ci_fix_run_id,
        "verdict": outcome.verdict.value,
        "committed_sha": outcome.committed_sha,
        "committed_branch": outcome.committed_branch,
        "escalation_reason": (
            outcome.escalation_reason.value if outcome.escalation_reason else None
        ),
    }


@celery_app.task(
    name="phalanx.agents.ci_fixer_v2.execute_v2_task",
    bind=True,
    queue="ci_fixer",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=0,  # v2 run bootstrap persists its own outcome; no blind retries
)
def execute_v2_task(self, ci_fix_run_id: str) -> dict:  # noqa: ARG001
    """Run the CI Fixer v2 bootstrap for one CIFixRun row.

    Returns a small dict summary suitable for Celery result storage.
    The real outcome (verdict, cost, fix metadata) is written to the
    CIFixRun row by `execute_v2_run` itself.
    """
    logger = log.bind(ci_fix_run_id=ci_fix_run_id, task="ci_fixer_v2")
    logger.info("v2.task.start")

    try:
        result = asyncio.run(_execute_v2_task_async(ci_fix_run_id))
    except Exception as exc:
        logger.error("v2.task.unhandled_error", error=str(exc), exc_info=True)
        raise

    logger.info(
        "v2.task.done",
        verdict=result["verdict"],
        escalation_reason=result["escalation_reason"],
        committed_sha=result["committed_sha"],
    )
    return result
