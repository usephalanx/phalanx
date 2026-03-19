"""
Skill ingestion scheduled tasks.
Polls configured skill feeds (GitHub releases, RSS, API docs) and upserts
SkillEntry rows so agents always have fresh reference material.
"""

import structlog

from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="forge.skills.ingestion.tasks.check_feeds", bind=True, max_retries=3)
def check_feeds(self) -> dict:  # pragma: no cover
    """
    Iterate skill-registry feed configs and pull new content.
    Dispatches individual ingest jobs to the `ingestion` queue.

    Fires daily via redbeat.
    """
    log.info("skills.ingestion.check_feeds.start")
    # TODO(M2): load skill-registry/*.yaml, check feed URLs,
    #           diff against last_ingested_at, dispatch per-skill ingest tasks.
    log.info("skills.ingestion.check_feeds.stub_noop")
    return {"dispatched": 0}
