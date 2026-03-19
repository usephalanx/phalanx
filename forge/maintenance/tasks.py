"""
Maintenance scheduled tasks.
Detects runs stuck in non-terminal states and auto-cancels them after timeout.
"""

import structlog

from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="forge.maintenance.tasks.check_blocked_runs", bind=True, max_retries=3)
def check_blocked_runs(self) -> dict:  # pragma: no cover
    """
    Scan for runs stuck in PLANNING/IN_PROGRESS/REVIEW/AWAITING_APPROVAL
    beyond their configured timeout and transition them to FAILED.

    Fires every 30 minutes via redbeat.
    """
    log.info("maintenance.check_blocked_runs.start")
    # TODO(M3): implement — query DB for runs past max_run_duration_minutes
    #           from team guardrails, emit blocked_run_detected event, cancel.
    log.info("maintenance.check_blocked_runs.stub_noop")
    return {"cancelled": 0}
