"""
CI webhook ingest routes.

Receives failure events from CI providers, normalizes them to CIFailureEvent,
creates a CIFixRun record, and dispatches the ci_fixer Celery task.

Endpoints:
  POST /webhook/github     — GitHub App check_run events
  POST /webhook/buildkite  — Buildkite build.finished events
  POST /webhook/circleci   — CircleCI workflow events (Phase 2)
  POST /webhook/jenkins    — Jenkins build events (Phase 2)

All endpoints verify webhook signatures before processing.
A 200 response is returned immediately — processing is async via Celery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import and_, select

from phalanx.ci_fixer.classifier import classify_failure
from phalanx.ci_fixer.events import CIFailureEvent
from phalanx.config.settings import get_settings
from phalanx.db.models import CIFixRun, CIIntegration
from phalanx.db.session import get_db

# Phase 3: commit-window dedup — if the same (repo, commit_sha) triggered a fix
# run within this window, suppress the duplicate.  Prevents webhook retries from
# spawning multiple fix runs for the same commit.
_COMMIT_DEDUP_WINDOW_MINUTES = 5

log = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter(tags=["ci-webhooks"])


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _dispatch_ci_fix(event: CIFailureEvent) -> CIFixRun | None:
    """
    Look up the CIIntegration for this repo, create a CIFixRun, dispatch task.
    Returns None if no integration found or max_attempts reached.
    """
    from phalanx.agents.ci_fixer import (
        execute_task,  # noqa: PLC0415 (avoid circular at module level)
    )

    async with get_db() as session:
        # Find matching integration
        result = await session.execute(
            select(CIIntegration).where(
                CIIntegration.repo_full_name == event.repo_full_name,
                CIIntegration.enabled == True,  # noqa: E712
            )
        )
        integration = result.scalar_one_or_none()

        if integration is None:
            log.info(
                "ci_webhook.no_integration",
                repo=event.repo_full_name,
                provider=event.provider,
            )
            return None

        # Author filter — if allowed_authors is set, skip PRs not in the list
        if (
            integration.allowed_authors
            and event.pr_author
            and event.pr_author not in integration.allowed_authors
        ):
            log.info(
                "ci_webhook.author_filtered",
                repo=event.repo_full_name,
                author=event.pr_author,
                allowed=integration.allowed_authors,
            )
            return None

        # Guard: don't re-fix the same build
        result = await session.execute(
            select(CIFixRun).where(
                CIFixRun.ci_build_id == event.build_id,
                CIFixRun.ci_provider == event.provider,
            )
        )
        if result.scalar_one_or_none():
            log.info("ci_webhook.already_processing", build_id=event.build_id)
            return None

        # Phase 3: commit-window dedup — prevent retried webhooks from spawning
        # duplicate runs for the same (repo, commit_sha) within 5 minutes.
        window_start = datetime.now(UTC) - timedelta(minutes=_COMMIT_DEDUP_WINDOW_MINUTES)
        result = await session.execute(
            select(CIFixRun).where(
                and_(
                    CIFixRun.repo_full_name == event.repo_full_name,
                    CIFixRun.commit_sha == event.commit_sha,
                    CIFixRun.created_at >= window_start,
                )
            )
        )
        if result.scalar_one_or_none():
            log.info(
                "ci_webhook.commit_window_dedup",
                repo=event.repo_full_name,
                commit_sha=event.commit_sha,
                window_minutes=_COMMIT_DEDUP_WINDOW_MINUTES,
            )
            return None

        # Check attempt count for this branch — max_attempts guard
        result = await session.execute(
            select(CIFixRun).where(
                CIFixRun.integration_id == integration.id,
                CIFixRun.branch == event.branch,
                CIFixRun.status.in_(["FIXED", "FAILED"]),
            )
        )
        prior_attempts = len(result.scalars().all())
        if prior_attempts >= integration.max_attempts:
            log.info(
                "ci_webhook.max_attempts_reached",
                repo=event.repo_full_name,
                branch=event.branch,
                attempts=prior_attempts,
            )
            return None

        # Classify from webhook payload if possible
        category = classify_failure(event.raw_payload.get("_log_preview", ""))

        # Create CIFixRun
        ci_run = CIFixRun(
            integration_id=integration.id,
            repo_full_name=event.repo_full_name,
            branch=event.branch,
            pr_number=event.pr_number,
            commit_sha=event.commit_sha,
            ci_provider=event.provider,
            ci_build_id=event.build_id,
            build_url=event.build_url,
            failed_jobs=event.failed_jobs or [],
            failure_summary=event.raw_payload.get("_log_preview", "")[:2000],
            failure_category=category if category != "unknown" else None,
            status="PENDING",
            attempt=prior_attempts + 1,
        )
        session.add(ci_run)
        await session.commit()
        await session.refresh(ci_run)

    # Dispatch async — return immediately to webhook caller
    execute_task.apply_async(
        args=[ci_run.id],
        queue="ci_fixer",
    )

    log.info(
        "ci_webhook.dispatched",
        ci_fix_run_id=ci_run.id,
        repo=event.repo_full_name,
        branch=event.branch,
        provider=event.provider,
    )
    return ci_run


def _verify_github_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify X-Hub-Signature-256 from GitHub webhook."""
    if not secret:
        return True  # signature check disabled (dev mode)
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _verify_buildkite_signature(body: bytes, token: str, stored_token: str) -> bool:
    """Verify X-Buildkite-Token from Buildkite webhook."""
    if not stored_token:
        return True
    return hmac.compare_digest(token or "", stored_token)


# ── GitHub App webhook ─────────────────────────────────────────────────────────


@router.post("/webhook/github", status_code=status.HTTP_200_OK)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    """
    Receives GitHub App webhook events.

    Handles:
    - check_run completed + conclusion=failure → dispatch CI fix
    - check_suite completed + conclusion=failure → dispatch CI fix
    """
    body = await request.body()

    # Verify signature
    if not _verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        log.warning("ci_webhook.github.invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)

    # Only process check_run completion failures
    if x_github_event == "check_run":
        action = payload.get("action")
        conclusion = payload.get("check_run", {}).get("conclusion")

        if action != "completed" or conclusion not in ("failure", "timed_out"):
            return {"status": "ignored", "reason": f"action={action} conclusion={conclusion}"}

        check_run = payload["check_run"]
        repo = payload["repository"]

        # Skip infrastructure/runner check runs (Node.js deprecation warnings, etc.)
        # Only process named CI gates that represent actual code failures
        check_name = check_run.get("name", "")
        skip_patterns = ("node", "set up job", "complete job", "post ", "initialize ")
        if any(check_name.lower().startswith(p) for p in skip_patterns):
            return {"status": "ignored", "reason": f"infrastructure check_run: {check_name}"}

        # Extract PR number and author from check_suite pull_requests
        pr_number: int | None = None
        pull_requests = check_run.get("check_suite", {}).get("pull_requests", [])
        if pull_requests:
            pr_number = pull_requests[0].get("number")

        # pr_author: prefer PR user login, fall back to sender (whoever triggered the event)
        pr_author: str | None = None
        if pull_requests:
            pr_author = pull_requests[0].get("user", {}).get("login") or pull_requests[0].get(
                "head", {}
            ).get("user", {}).get("login")
        if not pr_author:
            pr_author = payload.get("sender", {}).get("login")

        event = CIFailureEvent(
            provider="github_actions",
            repo_full_name=repo["full_name"],
            branch=check_run["check_suite"]["head_branch"] or "",
            commit_sha=check_run["head_sha"],
            build_id=str(check_run["id"]),
            build_url=check_run.get("details_url", ""),
            failed_jobs=[check_run["name"]],
            pr_number=pr_number,
            pr_author=pr_author,
            raw_payload=payload,
        )

        ci_run = await _dispatch_ci_fix(event)
        return {
            "status": "dispatched" if ci_run else "skipped",
            "ci_fix_run_id": ci_run.id if ci_run else None,
        }

    return {"status": "ignored", "event": x_github_event}


# ── Buildkite webhook ──────────────────────────────────────────────────────────


@router.post("/webhook/buildkite", status_code=status.HTTP_200_OK)
async def buildkite_webhook(
    request: Request,
    x_buildkite_token: str = Header(default=""),
):
    """
    Receives Buildkite webhook events.

    Handles:
    - build.finished with state=failed → dispatch CI fix

    Setup: Buildkite → Settings → Notification Services → Webhook
    Add URL: https://api.usephalanx.com/webhook/buildkite
    Events: build.finished
    Token: set X-Buildkite-Token to a secret shared with Phalanx.
    """
    body = await request.body()
    payload = json.loads(body)

    # Verify token (per-integration check — simplified here as global token)
    # TODO Phase 2: per-repo token lookup from CIIntegration
    buildkite_webhook_token = getattr(settings, "buildkite_webhook_token", "")
    if not _verify_buildkite_signature(body, x_buildkite_token, buildkite_webhook_token):
        log.warning("ci_webhook.buildkite.invalid_token")
        raise HTTPException(status_code=401, detail="Invalid Buildkite token")

    event_name = payload.get("event")
    if event_name != "build.finished":
        return {"status": "ignored", "event": event_name}

    build = payload.get("build", {})
    if build.get("state") not in ("failed", "canceled"):
        return {"status": "ignored", "state": build.get("state")}

    # Extract repo from pipeline
    pipeline = payload.get("pipeline", {})
    repo_url = pipeline.get("repository", "")
    # Convert git URL to owner/repo
    repo_full_name = _parse_repo_name(repo_url)
    if not repo_full_name:
        return {"status": "skipped", "reason": "cannot parse repo name"}

    failed_jobs = [j["name"] for j in build.get("jobs", []) if j.get("state") == "failed"]

    event = CIFailureEvent(
        provider="buildkite",
        repo_full_name=repo_full_name,
        branch=build.get("branch", ""),
        commit_sha=build.get("commit", ""),
        build_id=str(build.get("id", "")),
        build_url=build.get("web_url", ""),
        failed_jobs=failed_jobs,
        pr_number=build.get("pull_request", {}).get("id") if build.get("pull_request") else None,
        raw_payload=payload,
    )

    ci_run = await _dispatch_ci_fix(event)
    return {
        "status": "dispatched" if ci_run else "skipped",
        "ci_fix_run_id": ci_run.id if ci_run else None,
    }


# ── CircleCI webhook (Phase 2 stub) ────────────────────────────────────────────


@router.post("/webhook/circleci", status_code=status.HTTP_200_OK)
async def circleci_webhook(request: Request):
    """CircleCI webhook — Phase 2."""
    return {"status": "coming_soon", "provider": "circleci"}


# ── Jenkins webhook (Phase 2 stub) ─────────────────────────────────────────────


@router.post("/webhook/jenkins", status_code=status.HTTP_200_OK)
async def jenkins_webhook(request: Request):
    """Jenkins webhook — Phase 2."""
    return {"status": "coming_soon", "provider": "jenkins"}


# ── Short-path aliases (router is mounted at /webhook, so /github → /webhook/github) ───────────


@router.post("/github", status_code=status.HTTP_200_OK)
async def github_webhook_alias(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    """Alias for /webhook/github — correct path when router is mounted at /webhook prefix."""
    return await github_webhook(request, x_hub_signature_256, x_github_event)


@router.post("/buildkite", status_code=status.HTTP_200_OK)
async def buildkite_webhook_alias(
    request: Request,
    x_buildkite_token: str = Header(default=""),
):
    """Alias for /webhook/buildkite."""
    return await buildkite_webhook(request, x_buildkite_token)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _parse_repo_name(repo_url: str) -> str | None:
    """Extract 'owner/repo' from a git remote URL."""
    import re  # noqa: PLC0415

    # https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    if m:
        return m.group(1)
    return None
