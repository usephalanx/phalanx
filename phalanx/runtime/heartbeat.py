"""Task heartbeat — long-running agents stamp progress so the stuck-
task detector can tell "still working" from "Celery hung".

Wire-up pattern (per-agent):

    from phalanx.runtime.heartbeat import record_heartbeat

    # At a known progress checkpoint inside the agent's main loop:
    await record_heartbeat(task_id)

Heartbeats update Task.last_heartbeat_at via a single UPDATE; the
function never raises — heartbeat persistence failures must NOT take
down the agent's actual work. The stuck-task detector reads the column
periodically (default 2 min cycle) and flags any IN_PROGRESS task
whose heartbeat is older than its ttl_seconds (or the per-role default
below).

Per-role defaults match each agent's known progress cadence with a
2-3x safety margin:
  - cifix_techlead:   90s   (LLM turn ~5-30s; gives ~3-18 turns of slack)
  - cifix_engineer:  180s   (sandbox steps can take 2-3 min)
  - cifix_sre:       300s   (env detection + image build can stretch)
  - cifix_challenger: 60s   (Sonnet adversarial review is fast)
  - cifix_commander: 120s   (commander polls; should heartbeat each loop)
  - default:         180s   (conservative; covers unknown agents)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import update

from phalanx.db.models import Task
from phalanx.db.session import get_db

log = structlog.get_logger(__name__)


_DEFAULT_TTL_BY_ROLE: dict[str, int] = {
    "cifix_techlead": 90,
    "cifix_engineer": 180,
    "cifix_sre": 300,
    "cifix_sre_setup": 300,
    "cifix_sre_verify": 300,
    "cifix_challenger": 60,
    "cifix_commander": 120,
}
DEFAULT_TTL_SECONDS = 180


def default_ttl_for_role(role: str | None) -> int:
    """Resolve the heartbeat-staleness budget for `role`."""
    if role and role in _DEFAULT_TTL_BY_ROLE:
        return _DEFAULT_TTL_BY_ROLE[role]
    return DEFAULT_TTL_SECONDS


async def record_heartbeat(
    task_id: str,
    *,
    note: str | None = None,
) -> bool:
    """Stamp Task.last_heartbeat_at = NOW(). Always returns silently.

    Returns True on success, False on persistence failure. Callers can
    ignore the return value — the heartbeat path must never block agent
    progress on DB hiccups.
    """
    try:
        async with get_db() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(last_heartbeat_at=datetime.now(UTC))
            )
            await session.commit()
        log.debug("runtime.heartbeat", task_id=task_id, note=note)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "runtime.heartbeat_persist_failed",
            task_id=task_id,
            error=str(exc),
        )
        return False


async def set_initial_heartbeat_and_ttl(
    task_id: str,
    *,
    role: str | None = None,
    ttl_seconds: int | None = None,
) -> bool:
    """At task start, mark IN_PROGRESS + initial heartbeat + TTL.

    The stuck-task detector keys off `last_heartbeat_at` plus
    `ttl_seconds` (with per-role default fallback). Setting both at
    start means a worker that crashes before its first heartbeat is
    still detectable on the next sweep — `last_heartbeat_at` was set
    here, and any silence beyond TTL is staleness.
    """
    resolved_ttl = ttl_seconds if ttl_seconds is not None else default_ttl_for_role(role)
    try:
        async with get_db() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    last_heartbeat_at=datetime.now(UTC),
                    ttl_seconds=resolved_ttl,
                )
            )
            await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "runtime.heartbeat_initial_failed",
            task_id=task_id,
            error=str(exc),
        )
        return False


def is_stale(
    *,
    last_heartbeat_at: datetime | None,
    ttl_seconds: int | None,
    role: str | None = None,
    now: datetime | None = None,
) -> bool:
    """True iff the task's last heartbeat is older than its TTL.

    Pure function — no DB access. Used by the stuck detector after it
    SELECTs the relevant rows.
    """
    if last_heartbeat_at is None:
        # Heartbeat never set. Don't flag — initial-stamp path is
        # responsible for the first heartbeat. If the agent crashed
        # BEFORE set_initial_heartbeat_and_ttl ran, the row's
        # started_at vs NOW() check upstream catches it.
        return False
    ttl = ttl_seconds if ttl_seconds is not None else default_ttl_for_role(role)
    now = now or datetime.now(UTC)
    age_s = (now - last_heartbeat_at).total_seconds()
    return age_s > ttl


def heartbeat_age_seconds(
    last_heartbeat_at: datetime | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Seconds since last heartbeat, or None if never beat."""
    if last_heartbeat_at is None:
        return None
    now = now or datetime.now(UTC)
    return (now - last_heartbeat_at).total_seconds()
