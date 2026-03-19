"""
Skill staleness scheduled tasks.
Flags skills whose ingested content hasn't been refreshed within the
configured staleness window so the team config loader can alert.
"""

import structlog

from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="forge.skills.tasks.check_staleness", bind=True, max_retries=3)
def check_staleness(self) -> dict:  # pragma: no cover
    """
    Compare each skill's last_ingested_at against its configured
    max_staleness_days.  Emit a structured warning log for any stale skill
    so ops dashboards can surface alerts.

    Fires every 3 days via redbeat.
    """
    log.info("skills.check_staleness.start")
    # TODO(M2): load skill-registry/*.yaml, query skill_entries for staleness,
    #           log warning per stale skill with last_ingested_at delta.
    log.info("skills.check_staleness.stub_noop")
    return {"stale": 0}
