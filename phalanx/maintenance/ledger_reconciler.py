"""P0-6 — shadow ledger reconciliation.

The CLI writes a terminal ledger row at the end of its poll-wait. If the
CLI gave up before the run reached a true terminal state (e.g. SRE setup
hung, watchdog later marked the run FAILED_INFRA_TIMEOUT), the ledger
row is stale: it claims SAFE_ESCALATE but the underlying run completed
differently. P0-5 added provenance so we can DETECT this; this module
HEALS it.

Authoritative finalizer
-----------------------
The watchdog is the source of truth for terminal run state. This task
runs as a beat-scheduled celery task on `forge-worker` every 120s, finds
shadow_ledger rows that need reconciliation, recomputes the verdict
from the *current* run + task state, and updates the row.

Reconciliation is idempotent: a row already at its correct terminal
state is a no-op. The `previous_verdict` / `previous_failure_class`
columns preserve the original snapshot for audit; the new
`reconciled_at` / `reconciled_reason` columns mark the heal.

What gets reconciled
--------------------
Three shapes qualify as "needs reconciliation":

  A. ledger.phalanx_verdict == 'PENDING' AND linked run is terminal
     → CLI exited with RUN_STILL_ACTIVE; finalize from current state.

  B. ledger.phalanx_verdict in (SAFE_ESCALATE, SHIPPED_PROPOSED, FAILED)
     AND provenance.tl_task_status in (PENDING, IN_PROGRESS, CANCELLED)
     AND linked run is terminal
     → ill-formed snapshot; replace with current state.

  C. ledger.failure_class differs from runs.failure_class
     AND linked run is terminal AND the difference is meaningful
     (e.g. NULL → FAILED_INFRA_TIMEOUT)
     → run state evolved after snapshot; update.

Never touches:
  - rows whose linked run is non-terminal (no truth to reconcile against)
  - rows that were already reconciled at a state matching the current
    run state (idempotent no-op)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import and_, or_, select

from phalanx.db.models import Run, ShadowLedger, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app
from phalanx.shadow import provenance as prov_mod
from phalanx.shadow.ledger import to_dict
from phalanx.shadow.ledger_export import append_ledger_row_async

log = structlog.get_logger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({
    "VERIFYING", "FAILED", "CANCELLED", "TIMED_OUT", "COMPLETED",
})

_VERDICT_NEEDS_TERMINAL_TL = frozenset({"SAFE_ESCALATE"})


@celery_app.task(
    name="phalanx.maintenance.ledger_reconciler.reconcile_shadow_ledger",
    bind=True,
    max_retries=1,
    acks_late=True,
    soft_time_limit=240,
    time_limit=300,
)
def reconcile_shadow_ledger(self) -> dict:  # pragma: no cover
    """Celery entry point. Runs every 120s via beat. Idempotent."""
    return asyncio.run(_reconcile_impl())


async def _reconcile_impl() -> dict[str, Any]:
    candidates = await _find_candidates()
    reconciled = 0
    no_op = 0
    failed = 0
    for row_id in candidates:
        try:
            healed = await _reconcile_one(row_id)
            if healed:
                reconciled += 1
            else:
                no_op += 1
        except Exception as e:  # noqa: BLE001
            log.exception(
                "ledger_reconciler.reconcile_one_failed",
                ledger_id=row_id, error=str(e),
            )
            failed += 1
    summary = {
        "candidates": len(candidates),
        "reconciled": reconciled,
        "no_op": no_op,
        "failed": failed,
    }
    if candidates:
        log.info("ledger_reconciler.cycle", **summary)
    return summary


async def _find_candidates() -> list[str]:
    """Return ledger_ids needing reconciliation. Conservative: only rows
    whose linked run is in a terminal state."""
    async with get_db() as session:
        # All ledger rows with a linked terminal run.
        # We do a coarse query and refine in Python because the
        # provenance JSONB extraction is awkward in raw SQLAlchemy.
        stmt = (
            select(ShadowLedger.id)
            .join(Run, Run.id == ShadowLedger.phalanx_run_id)
            .where(Run.status.in_(list(_TERMINAL_RUN_STATUSES)))
            .order_by(ShadowLedger.created_at.asc())
        )
        result = await session.execute(stmt)
        candidate_ids = [row[0] for row in result.all()]

    # Refine: keep only rows that actually need reconciliation.
    needs: list[str] = []
    for lid in candidate_ids:
        if await _needs_reconciliation(lid):
            needs.append(lid)
    return needs


async def _needs_reconciliation(ledger_id: str) -> bool:
    """Return True iff this ledger row is stale relative to its run."""
    async with get_db() as session:
        ledger = await session.get(ShadowLedger, ledger_id)
        if ledger is None or ledger.phalanx_run_id is None:
            return False
        run = await session.get(Run, ledger.phalanx_run_id)
        if run is None or run.status not in _TERMINAL_RUN_STATUSES:
            return False

    # Shape A: PENDING ledger + terminal run.
    if ledger.phalanx_verdict == "PENDING":
        return True

    # Shape B: verdict claims TL refused, provenance shows TL non-terminal.
    if ledger.phalanx_verdict in _VERDICT_NEEDS_TERMINAL_TL:
        prov = ledger.phalanx_provenance or {}
        tl_status = prov.get("tl_task_status")
        tl_count = prov.get("tl_task_count") or 0
        if tl_count > 0 and tl_status in prov_mod._TL_NON_TERMINAL_STATUSES:
            return True

    # Shape C: failure_class evolved on the run after snapshot.
    if (
        run.failure_class
        and run.failure_class != ledger.failure_class
        and ledger.phalanx_verdict != "FAILED"
    ):
        return True

    return False


async def _reconcile_one(ledger_id: str) -> bool:
    """Recompute the terminal state from the run + tasks. Update the
    ledger row if it differs. Returns True iff any change was written."""
    # Import locally to avoid circular imports at module load time.
    from phalanx.shadow.runner import (  # noqa: PLC0415
        _classify_verdict,
        _read_terminal_evidence,
        _resolve_failure_class,
    )

    async with get_db() as session:
        ledger = await session.get(ShadowLedger, ledger_id)
        if ledger is None or ledger.phalanx_run_id is None:
            return False
        run = await session.get(Run, ledger.phalanx_run_id)
        if run is None or run.status not in _TERMINAL_RUN_STATUSES:
            return False
        run_id = ledger.phalanx_run_id
        # Capture original snapshot fields BEFORE recomputing so we can
        # preserve them via previous_*.
        original_verdict = ledger.phalanx_verdict
        original_failure_class = ledger.failure_class
        # If this row was already reconciled, that's its true previous;
        # don't overwrite.
        previous_verdict = (
            ledger.previous_verdict
            if ledger.previous_verdict is not None
            else original_verdict
        )
        previous_failure_class = (
            ledger.previous_failure_class
            if ledger.previous_failure_class is not None
            else original_failure_class
        )

    # Recompute terminal evidence using the same helpers the CLI uses.
    evidence = await _read_terminal_evidence(run_id)
    tl = evidence["tl_output"]
    eng = evidence["engineer_output"]
    tasks = evidence["tasks"]
    new_verdict, classification_reason = _classify_verdict(
        run_status=run.status, tl=tl, eng=eng, tasks=tasks,
    )
    new_failure_class, sre_diag = await _resolve_failure_class(run_id, tasks)

    new_confidence = (
        float(tl.get("confidence") or 0.0)
        if isinstance(tl, dict) and tl.get("confidence") is not None
        else None
    )
    new_root_cause = tl.get("root_cause") if isinstance(tl, dict) else None

    # P0-5 / P1-6 — synthesize root_cause when applicable.
    rc_synthesized = False
    rc_synthesis_reason: str | None = None
    if new_verdict == "SAFE_ESCALATE" and not (new_root_cause or "").strip():
        new_root_cause = prov_mod.synthesize_root_cause_for_safe_escalate(
            classification_reason, tl if isinstance(tl, dict) else {}
        )
        rc_synthesized = True
        rc_synthesis_reason = classification_reason or "unspecified"
    elif (
        new_verdict == "FAILED"
        and not (new_root_cause or "").strip()
        and sre_diag is not None
    ):
        new_root_cause = prov_mod.synthesize_root_cause_for_sandbox_setup(sre_diag)
        rc_synthesized = True
        rc_synthesis_reason = "sandbox_setup_failed"

    new_provenance = prov_mod.build_provenance(
        tasks,
        root_cause_synthesized=rc_synthesized,
        root_cause_synthesis_reason=rc_synthesis_reason,
        sre_setup_diagnostic=sre_diag,
    )

    # Decide: is the new state different from the current ledger state?
    # If everything matches, this is a no-op (idempotent).
    if (
        original_verdict == new_verdict
        and original_failure_class == new_failure_class
        and (ledger.phalanx_provenance or {}).get("tl_task_id") ==
            new_provenance.get("tl_task_id")
        and (ledger.phalanx_provenance or {}).get("tl_task_status") ==
            new_provenance.get("tl_task_status")
    ):
        return False

    reason = _reconciliation_reason(
        original_verdict=original_verdict,
        original_failure_class=original_failure_class,
        new_verdict=new_verdict,
        new_failure_class=new_failure_class,
        run=run,
    )

    async with get_db() as session:
        ledger = await session.get(ShadowLedger, ledger_id)
        ledger.phalanx_verdict = new_verdict
        ledger.failure_class = new_failure_class
        ledger.phalanx_confidence = new_confidence
        ledger.phalanx_root_cause = new_root_cause
        ledger.phalanx_provenance = new_provenance
        ledger.reconciled_at = datetime.now(timezone.utc)
        ledger.reconciled_reason = reason
        ledger.previous_verdict = previous_verdict
        ledger.previous_failure_class = previous_failure_class
        await session.commit()
        await session.refresh(ledger)
        # P0-2 — JSONL export AFTER commit. Failure logged, never raised.
        await append_ledger_row_async(to_dict(ledger), exported_by="reconcile")
        # Path B (2026-05-20) — maintainer-comment delivery on reconciled
        # terminals. Same opt-in + idempotency + suppress contract as the
        # main writer path. Idempotency means double-firing (writer +
        # reconciler) is safe.
        try:
            from phalanx.db.models import CIIntegration  # noqa: PLC0415
            from phalanx.shadow.maintainer_comments import (  # noqa: PLC0415
                post_maintainer_comment_async,
            )
            integ = (await session.execute(
                select(CIIntegration).where(CIIntegration.repo_full_name == ledger.repo)
            )).scalar_one_or_none()
            if integ is not None:
                await post_maintainer_comment_async(
                    row_dict=to_dict(ledger),
                    integration_enabled=bool(getattr(
                        integ, "maintainer_comments_enabled", False,
                    )),
                    integration_token=integ.github_token,
                )
        except Exception:  # noqa: BLE001
            pass

    log.info(
        "ledger_reconciler.reconciled",
        ledger_id=ledger_id,
        run_id=run_id,
        reason=reason,
        previous_verdict=previous_verdict,
        new_verdict=new_verdict,
        previous_failure_class=previous_failure_class,
        new_failure_class=new_failure_class,
    )
    return True


def _reconciliation_reason(
    *,
    original_verdict: str | None,
    original_failure_class: str | None,
    new_verdict: str | None,
    new_failure_class: str | None,
    run: Run,
) -> str:
    """One-line, machine-readable reason for the heal. ≤80 chars."""
    if original_verdict == "PENDING":
        return "cli_left_pending_run_terminal"
    if (
        original_failure_class != new_failure_class
        and new_failure_class
        and "INFRA" in new_failure_class
    ):
        return f"watchdog_marked_{new_failure_class.lower()}"
    if original_verdict != new_verdict:
        return f"ill_formed_snapshot_replaced_{original_verdict}_to_{new_verdict}"[:80]
    return "snapshot_evolved_with_run_state"
