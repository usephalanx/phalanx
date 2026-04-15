"""
CI Fix Pattern Promoter — Phase 5.

Promotes a fix pattern from repo-local (CIFailureFingerprint) to cross-repo
registry (CIPatternRegistry) when:
  1. The pattern has succeeded in >= MIN_REPOS_FOR_PROMOTION distinct repos, OR
  2. The pattern has succeeded >= MIN_GLOBAL_SUCCESS_COUNT times in one repo
     (single-repo promotion for high-confidence patterns)

Once in the registry, the pattern is available to ALL repos as a suggestion
(never auto-applied across repos — that requires explicit opt-in per-repo).

The promoter runs as a Celery beat task every hour.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select, update

from phalanx.db.models import CIFailureFingerprint, CIPatternRegistry
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

# A pattern must appear in this many distinct repos before cross-repo promotion.
MIN_REPOS_FOR_PROMOTION = 2
# Or have this many successes in a single repo (high confidence, single-repo).
MIN_GLOBAL_SUCCESS_COUNT = 10


async def _promote_patterns() -> None:
    """
    Main async body: find promotable fingerprints and upsert into registry.
    """
    # Find fingerprints with enough success to promote
    async with get_db() as session:
        # Aggregated across all repos: group by fingerprint_hash
        result = await session.execute(
            select(
                CIFailureFingerprint.fingerprint_hash,
                CIFailureFingerprint.tool,
                CIFailureFingerprint.sample_errors,
                CIFailureFingerprint.last_good_patch_json,
                func.count(CIFailureFingerprint.repo_full_name.distinct()).label("repo_count"),
                func.sum(CIFailureFingerprint.success_count).label("total_successes"),
            ).group_by(
                CIFailureFingerprint.fingerprint_hash,
                CIFailureFingerprint.tool,
                CIFailureFingerprint.sample_errors,
                CIFailureFingerprint.last_good_patch_json,
            )
        )
        rows = result.all()

    promoted = 0
    for row in rows:
        fingerprint_hash = row.fingerprint_hash
        tool = row.tool
        repo_count = row.repo_count or 0
        total_successes = row.total_successes or 0

        if repo_count < MIN_REPOS_FOR_PROMOTION and total_successes < MIN_GLOBAL_SUCCESS_COUNT:
            continue

        # Check if already in registry
        async with get_db() as session:
            result = await session.execute(
                select(CIPatternRegistry).where(
                    CIPatternRegistry.fingerprint_hash == fingerprint_hash
                )
            )
            existing = result.scalar_one_or_none()

            now = datetime.now(UTC)
            if existing is None:
                entry = CIPatternRegistry(
                    id=str(uuid.uuid4()),
                    fingerprint_hash=fingerprint_hash,
                    tool=tool,
                    description=row.sample_errors or "",
                    patch_template_json=row.last_good_patch_json,
                    repo_count=repo_count,
                    total_success_count=total_successes,
                    promoted_at=now,
                    updated_at=now,
                )
                session.add(entry)
                promoted += 1
                log.info(
                    "pattern_promoter.promoted",
                    fingerprint=fingerprint_hash,
                    tool=tool,
                    repo_count=repo_count,
                    total_successes=total_successes,
                )
            else:
                # Update counters
                await session.execute(
                    update(CIPatternRegistry)
                    .where(CIPatternRegistry.id == existing.id)
                    .values(
                        repo_count=repo_count,
                        total_success_count=total_successes,
                        patch_template_json=row.last_good_patch_json,
                        updated_at=now,
                    )
                )

            await session.commit()

    log.info("pattern_promoter.done", promoted=promoted, checked=len(rows))


def is_promotion_eligible(
    repo_count: int,
    total_success_count: int,
) -> bool:
    """
    Pure function: return True if a fingerprint qualifies for registry promotion.

    Args:
        repo_count: distinct repos where this fix has succeeded
        total_success_count: total successful applications across all repos
    """
    return repo_count >= MIN_REPOS_FOR_PROMOTION or total_success_count >= MIN_GLOBAL_SUCCESS_COUNT


# ── Celery task ────────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.ci_fixer.pattern_promoter.promote_patterns",
    queue="ci_fixer",
    soft_time_limit=60,
    time_limit=90,
)
def promote_patterns() -> None:
    """Celery beat task: promote eligible patterns to the cross-repo registry."""
    try:
        asyncio.run(_promote_patterns())
    except Exception:
        log.exception("pattern_promoter.task_unhandled")
        raise
