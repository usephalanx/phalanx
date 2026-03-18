"""
Memory scheduled tasks.
Decays relevance scores of old memory entries to keep context windows lean.
"""
import structlog
from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="forge.memory.tasks.decay_relevance", bind=True, max_retries=3)
def decay_relevance(self) -> dict:  # pragma: no cover
    """
    Apply exponential decay to relevance_score on MemoryEntry rows older
    than 7 days.  Entries below a floor threshold are archived (soft-deleted).

    Fires weekly via redbeat.
    """
    log.info("memory.decay_relevance.start")
    # TODO(M3): implement — UPDATE memory_entries SET relevance_score = relevance_score * 0.85
    #           WHERE created_at < NOW() - INTERVAL '7 days' AND archived_at IS NULL
    log.info("memory.decay_relevance.stub_noop")
    return {"decayed": 0, "archived": 0}
