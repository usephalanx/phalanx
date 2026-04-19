"""
CI Fix Outcome Tracker — Phase 2 learning loop.

After a fix PR is opened, we need to know whether it was merged (success),
closed without merging (fix was wrong), or left open (inconclusive).

Poll schedule per fix run (relative to fix PR creation):
  Poll 1:  4 hours  — enough time for human review on most teams
  Poll 2: 24 hours  — covers teams with async review cycles
  Poll 3: 72 hours  — final verdict; runs that are still open = inconclusive

On merge → increment success_count on CIFailureFingerprint.
On close-without-merge → increment failure_count.
After poll 3 → set outcome_checked=True on CIFixRun (no more polling).

The Celery beat task `poll_fix_outcomes` runs every 30 minutes and processes
all runs that:
  - Have a fix_pr_number (PR was opened)
  - Have outcome_checked=False (not finished polling)
  - Were created in the past 72 hours
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, select, update

from phalanx.db.models import CIFailureFingerprint, CIFixOutcome, CIFixRun
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

# Poll schedule: (poll_number, hours_after_creation)
_POLL_SCHEDULE = [
    (1, 4),
    (2, 24),
    (3, 72),
]
_FINAL_POLL = 3


async def _poll_all_pending() -> None:
    """
    Main async body of the beat task.
    Finds all runs needing outcome checks and processes them.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=72)

    async with get_db() as session:
        result = await session.execute(
            select(CIFixRun).where(
                and_(
                    CIFixRun.fix_pr_number.isnot(None),
                    CIFixRun.outcome_checked.is_(False),
                    CIFixRun.created_at >= cutoff,
                )
            )
        )
        runs = result.scalars().all()

    log.info("outcome_tracker.runs_to_check", count=len(runs))

    for run in runs:
        try:
            await _process_run(run, now)
        except Exception as exc:
            log.warning("outcome_tracker.run_failed", run_id=run.id, error=str(exc))


async def _process_run(run: CIFixRun, now: datetime) -> None:
    """
    Determine which polls are due for this run and execute them.
    Marks outcome_checked=True after the final poll.
    """
    if run.created_at is None:
        return

    created = run.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)

    elapsed_hours = (now - created).total_seconds() / 3600

    # Which polls have already been recorded?
    async with get_db() as session:
        result = await session.execute(
            select(CIFixOutcome.poll_number).where(CIFixOutcome.ci_fix_run_id == run.id)
        )
        done_polls = {row[0] for row in result.all()}

    for poll_num, threshold_hours in _POLL_SCHEDULE:
        if poll_num in done_polls:
            continue
        if elapsed_hours < threshold_hours:
            continue

        # This poll is due — fetch PR state and record outcome
        outcome = await _check_pr_outcome(run)
        await _record_outcome(run, poll_num, outcome)

        if outcome["outcome"] == "merged":
            await _update_fingerprint(run, success=True)
        elif outcome["outcome"] == "closed_unmerged":
            await _update_fingerprint(run, success=False)

        if poll_num == _FINAL_POLL:
            await _mark_outcome_checked(run)

        log.info(
            "outcome_tracker.poll_recorded",
            run_id=run.id,
            poll_number=poll_num,
            outcome=outcome["outcome"],
        )


async def _check_pr_outcome(run: CIFixRun) -> dict:
    """
    Query GitHub for the current state of the fix PR.
    Returns dict with keys: outcome, pr_state, merged_at, closed_at.

    Gracefully handles missing token or network failures — returns 'open'.
    """
    try:
        import httpx  # noqa: PLC0415

        token = await _get_github_token(run)
        if not token:
            return {"outcome": "open", "pr_state": "open", "merged_at": None, "closed_at": None}

        url = f"https://api.github.com/repos/{run.repo_full_name}/pulls/{run.fix_pr_number}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 404:
            return {"outcome": "not_found", "pr_state": None, "merged_at": None, "closed_at": None}

        resp.raise_for_status()
        data = resp.json()

        pr_state = data.get("state", "open")  # 'open' | 'closed'
        merged_at_str = data.get("merged_at")
        closed_at_str = data.get("closed_at")

        merged_at = _parse_iso(merged_at_str)
        closed_at = _parse_iso(closed_at_str)

        if merged_at is not None:
            outcome = "merged"
        elif pr_state == "closed":
            outcome = "closed_unmerged"
        else:
            outcome = "open"

        return {
            "outcome": outcome,
            "pr_state": pr_state,
            "merged_at": merged_at,
            "closed_at": closed_at,
        }

    except Exception as exc:
        log.warning("outcome_tracker.github_check_failed", run_id=run.id, error=str(exc))
        return {"outcome": "open", "pr_state": "open", "merged_at": None, "closed_at": None}


async def _get_github_token(run: CIFixRun) -> str | None:
    """Load GitHub token from the CIIntegration row for this run."""
    try:
        from phalanx.db.models import CIIntegration  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(
                select(CIIntegration).where(CIIntegration.id == run.integration_id)
            )
            integration = result.scalar_one_or_none()

        if integration is None:
            return None

        if integration.github_token:
            return integration.github_token

        if integration.ci_api_key_enc:
            # Decrypt if needed — same logic as CIFixerAgent._decrypt_key
            from phalanx.config.settings import get_settings  # noqa: PLC0415

            settings = get_settings()
            enc_key = getattr(settings, "encryption_key", None)
            if enc_key:
                try:
                    from cryptography.fernet import Fernet  # noqa: PLC0415

                    f = Fernet(enc_key.encode())
                    return f.decrypt(integration.ci_api_key_enc.encode()).decode()
                except Exception:
                    pass

        return None
    except Exception as exc:
        log.warning("outcome_tracker.token_load_failed", error=str(exc))
        return None


async def _record_outcome(run: CIFixRun, poll_number: int, outcome: dict) -> None:
    """Insert a CIFixOutcome row."""
    async with get_db() as session:
        row = CIFixOutcome(
            id=str(uuid.uuid4()),
            ci_fix_run_id=run.id,
            poll_number=poll_number,
            outcome=outcome["outcome"],
            pr_state=outcome.get("pr_state"),
            merged_at=outcome.get("merged_at"),
            closed_at=outcome.get("closed_at"),
            polled_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()


async def _update_fingerprint(run: CIFixRun, success: bool) -> None:
    """
    Upsert CIFailureFingerprint counters for this run's fingerprint.

    If no row exists yet for (fingerprint_hash, repo_full_name), create one.
    Otherwise increment the appropriate counter.
    """
    if not run.fingerprint_hash:
        return

    async with get_db() as session:
        result = await session.execute(
            select(CIFailureFingerprint).where(
                and_(
                    CIFailureFingerprint.fingerprint_hash == run.fingerprint_hash,
                    CIFailureFingerprint.repo_full_name == run.repo_full_name,
                )
            )
        )
        fp = result.scalar_one_or_none()

        if fp is None:
            fp = CIFailureFingerprint(
                id=str(uuid.uuid4()),
                fingerprint_hash=run.fingerprint_hash,
                repo_full_name=run.repo_full_name,
                tool=run.ci_provider,  # best proxy available without parsing again
                seen_count=1,
                success_count=1 if success else 0,
                failure_count=0 if success else 1,
                last_seen_at=datetime.now(UTC),
            )
            session.add(fp)
        else:
            fp.seen_count += 1
            fp.last_seen_at = datetime.now(UTC)
            if success:
                fp.success_count += 1
                # Store the winning patch for Phase 3 reuse
                if run.fix_commit_sha:
                    fp.last_good_tool_version = run.validation_tool_version
            else:
                fp.failure_count += 1

        await session.commit()


async def _mark_outcome_checked(run: CIFixRun) -> None:
    """Mark a CIFixRun as fully outcome-checked — no more polling."""
    async with get_db() as session:
        await session.execute(
            update(CIFixRun).where(CIFixRun.id == run.id).values(outcome_checked=True)
        )
        await session.commit()


def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string from GitHub API."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Celery task ────────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.ci_fixer.outcome_tracker.poll_fix_outcomes",
    queue="ci_fixer",
    soft_time_limit=120,
    time_limit=180,
)
def poll_fix_outcomes() -> None:
    """Celery beat task: poll GitHub for fix PR outcomes."""
    try:
        asyncio.run(_poll_all_pending())
    except Exception:
        log.exception("outcome_tracker.task_unhandled")
        raise
