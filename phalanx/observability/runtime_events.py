"""Structured runtime events — stable names, structured payload.

Every event below maps to a single structlog `info()` call with a
known set of kwargs. Naming is namespaced as `runtime.<event>` so a
log search like `event:runtime.task_timeout` finds every occurrence
across all agents.

The point is not to invent new logging — structlog already does that —
but to lock the event NAMES and payload SHAPES so dashboards / alerts
/ ledger writers depend on a stable interface.

Event taxonomy (matches v1.7.3 spec section 5):

  task_started      — agent task began executing (post-Celery dispatch)
  task_heartbeat    — agent stamped progress (also writes the column)
  task_timeout      — stuck detector marked task TIMED_OUT
  task_completed    — agent task ended COMPLETED
  task_failed       — agent task ended FAILED with non-infra reason
  run_finalized     — commander or detector wrote terminal Run.status
  sandbox_cleanup   — stop_sandbox called on terminal/timeout path
  queue_depth       — periodic queue-depth sample (best-effort)
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger("phalanx.runtime")


def task_started(
    *,
    task_id: str,
    run_id: str,
    agent_role: str,
    ttl_seconds: int,
) -> None:
    log.info(
        "runtime.task_started",
        task_id=task_id,
        run_id=run_id,
        agent_role=agent_role,
        ttl_seconds=ttl_seconds,
    )


def task_heartbeat(
    *,
    task_id: str,
    run_id: str,
    agent_role: str,
    note: str | None = None,
) -> None:
    log.info(
        "runtime.task_heartbeat",
        task_id=task_id,
        run_id=run_id,
        agent_role=agent_role,
        note=note,
    )


def task_timeout(
    *,
    task_id: str,
    run_id: str | None,
    agent_role: str,
    age_seconds: float,
    ttl_seconds: int,
) -> None:
    log.warning(
        "runtime.task_timeout",
        task_id=task_id,
        run_id=run_id,
        agent_role=agent_role,
        age_seconds=round(age_seconds, 1),
        ttl_seconds=ttl_seconds,
    )


def task_completed(
    *,
    task_id: str,
    run_id: str,
    agent_role: str,
    duration_seconds: float | None,
) -> None:
    log.info(
        "runtime.task_completed",
        task_id=task_id,
        run_id=run_id,
        agent_role=agent_role,
        duration_seconds=(
            round(duration_seconds, 1) if duration_seconds is not None else None
        ),
    )


def task_failed(
    *,
    task_id: str,
    run_id: str,
    agent_role: str,
    error: str,
    duration_seconds: float | None = None,
) -> None:
    log.warning(
        "runtime.task_failed",
        task_id=task_id,
        run_id=run_id,
        agent_role=agent_role,
        error=error[:300],
        duration_seconds=(
            round(duration_seconds, 1) if duration_seconds is not None else None
        ),
    )


def run_finalized(
    *,
    run_id: str,
    final_status: str,
    failure_class: str | None,
    reason: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    log.info(
        "runtime.run_finalized",
        run_id=run_id,
        final_status=final_status,
        failure_class=failure_class,
        reason=(reason or "")[:300] or None,
        duration_seconds=(
            round(duration_seconds, 1) if duration_seconds is not None else None
        ),
    )


def sandbox_cleanup(
    *,
    run_id: str | None,
    container_id: str | None,
    ok: bool,
    reason: str,
    error: str | None = None,
) -> None:
    log.info(
        "runtime.sandbox_cleanup",
        run_id=run_id,
        container_id=container_id,
        ok=ok,
        reason=reason,
        error=(error or "")[:300] or None,
    )


def queue_depth(
    *,
    queue: str,
    depth: int,
) -> None:
    log.info("runtime.queue_depth", queue=queue, depth=depth)
