"""v1.7.3 runtime hardening — guaranteed sandbox cleanup.

Containers must be stopped on every run-termination path:
  - SHIPPED         → cleanup
  - FAILED          → cleanup
  - TIMED_OUT       → cleanup (already triggered by stuck-task detector)
  - CANCELLED       → cleanup
  - Commander crash → caught by stuck-task detector → cleanup

This module provides ONE entry point — `cleanup_for_run(run_id)` — that
the commander, stuck detector, and any future terminal path can call.
It is always best-effort and never raises.

Cleanup steps:
  1. Find the latest cifix_sre / cifix_sre_setup task for the run.
  2. Pull container_id from its output JSONB.
  3. Call provisioner.stop_sandbox(container_id) with a timeout.
  4. Emit a `runtime.sandbox_cleanup` event with the result.

Idempotent. Calling twice on the same run hits Docker's `rm -f` which
silently no-ops on an already-removed container.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select

from phalanx.db.models import Task
from phalanx.db.session import get_db
from phalanx.observability.runtime_events import sandbox_cleanup as _emit_event

log = structlog.get_logger(__name__)


async def cleanup_for_run(
    run_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Best-effort: stop+remove the run's sandbox container.

    Always returns a result dict; never raises. The dict carries enough
    info to write to a ledger or audit log:

      {
        "ok": bool,
        "container_id": str | None,
        "reason": str,           # echo of input
        "error": str | None,
      }
    """
    out: dict[str, Any] = {
        "ok": False,
        "container_id": None,
        "reason": reason,
        "error": None,
    }

    container_id: str | None = None
    try:
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
            out["error"] = "no_sre_setup_output_found"
            _emit_event(
                run_id=run_id,
                container_id=None,
                ok=False,
                reason=reason,
                error="no_sre_setup_output_found",
            )
            return out

        container_id = row[0].get("container_id")
        if not container_id:
            out["error"] = "no_container_id_in_sre_output"
            _emit_event(
                run_id=run_id,
                container_id=None,
                ok=False,
                reason=reason,
                error="no_container_id_in_sre_output",
            )
            return out

        # Lazy import — provisioner pulls in docker / asyncio subprocess
        # machinery we don't want loaded at module-import time.
        from phalanx.ci_fixer_v3.provisioner import stop_sandbox  # noqa: PLC0415

        await stop_sandbox(container_id)
        out["ok"] = True
        out["container_id"] = container_id
        _emit_event(
            run_id=run_id,
            container_id=container_id,
            ok=True,
            reason=reason,
        )
        return out
    except Exception as exc:  # noqa: BLE001 — never propagate
        err = f"{type(exc).__name__}: {exc}"
        out["error"] = err
        _emit_event(
            run_id=run_id,
            container_id=container_id,
            ok=False,
            reason=reason,
            error=err,
        )
        return out
