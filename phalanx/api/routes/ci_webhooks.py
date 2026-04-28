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
from sqlalchemy import and_, select, update

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
    from phalanx.agents.ci_fixer_v2_task import (
        execute_v2_task,  # noqa: PLC0415
    )

    async with get_db() as session:
        # Bug #11 B2 — per-(repo, pr_number) advisory lock. Catches concurrent
        # webhook racing for the same PR (different check_suites of the same PR
        # arriving in parallel — A3's idempotency key only catches same suite).
        # Non-blocking try-lock; fall through if already held by another tx.
        # Lock auto-releases on commit/rollback (xact_lock variant).
        if event.pr_number is not None:
            from sqlalchemy import text  # noqa: PLC0415

            # PG advisory locks take a single bigint key. Hash (repo, pr) into
            # a stable int64. abs() so we stay in the signed-bigint range.
            lock_key = abs(hash((event.repo_full_name, event.pr_number))) % (2**63)
            result = await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
            )
            if not result.scalar():
                log.info(
                    "ci_webhook.dispatch_lock_held",
                    repo=event.repo_full_name,
                    pr_number=event.pr_number,
                )
                return None

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

        # Bug #11 A3: idempotency-key dedup. Before time-window logic, check
        # if a CIFixRun already exists for this exact check_suite. Multiple
        # check_runs of the same suite arrive separately but share suite.id.
        # The unique partial index `ci_fix_runs_repo_check_suite_idem` enforces
        # the constraint at the DB layer too — this query is the fast-path.
        if event.ci_check_suite_id is not None:
            result = await session.execute(
                select(CIFixRun).where(
                    CIFixRun.repo_full_name == event.repo_full_name,
                    CIFixRun.ci_check_suite_id == event.ci_check_suite_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                log.info(
                    "ci_webhook.check_suite_idem_dedup",
                    ci_fix_run_id=existing.id,
                    repo=event.repo_full_name,
                    check_suite_id=event.ci_check_suite_id,
                )
                return None

        # Create CIFixRun
        ci_run = CIFixRun(
            integration_id=integration.id,
            repo_full_name=event.repo_full_name,
            branch=event.branch,
            pr_number=event.pr_number,
            commit_sha=event.commit_sha,
            ci_provider=event.provider,
            ci_build_id=event.build_id,
            ci_check_suite_id=event.ci_check_suite_id,
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

    # ── Pipeline dispatch ────────────────────────────────────────────────────
    # Three generations coexist; integration.cifixer_version picks per-repo:
    #   'v3' → multi-agent DAG (cifix_commander, new, opt-in per repo)
    #   'v2' → single-agent loop + verification gate (default, current prod)
    #   otherwise → legacy v1 (still around for old integrations)
    if integration.cifixer_version == "v3":
        try:
            await _dispatch_ci_fix_v3(event=event, integration=integration, ci_run=ci_run)
            pipeline_version = "v3"
        except Exception as exc:
            # v3 dispatch failure falls back to v2 so the PR still gets fixed.
            # Log loudly — the v3 path needs investigation.
            log.exception(
                "ci_webhook.v3_dispatch_failed_falling_back",
                ci_fix_run_id=ci_run.id,
                repo=event.repo_full_name,
                error=str(exc),
            )
            execute_v2_task.apply_async(args=[ci_run.id], queue="ci_fixer")
            pipeline_version = "v2_fallback"
    elif settings.phalanx_ci_fixer_v2_enabled:
        execute_v2_task.apply_async(args=[ci_run.id], queue="ci_fixer")
        pipeline_version = "v2"
    else:
        execute_task.apply_async(args=[ci_run.id], queue="ci_fixer")
        pipeline_version = "v1"

    log.info(
        "ci_webhook.dispatched",
        ci_fix_run_id=ci_run.id,
        repo=event.repo_full_name,
        branch=event.branch,
        provider=event.provider,
        pipeline_version=pipeline_version,
    )
    return ci_run


async def _dispatch_ci_fix_v3(
    event: CIFailureEvent, integration: CIIntegration, ci_run: CIFixRun
) -> None:
    """v3 dispatch path: creates a WorkOrder + Run and fires cifix_commander.

    The CIFixRun row still exists (created by the caller) — it's kept as the
    de-dup marker so webhook retries don't spawn parallel v3 runs. v3 drives
    its own state through `runs` + `tasks` tables.
    """
    import uuid as _uuid

    from phalanx.db.models import Project, Run, WorkOrder
    from phalanx.runtime.task_router import TaskRouter
    from phalanx.queue.celery_app import celery_app as _celery

    # Resolve or lazily create a system Project for CI-fix work orders.
    # Each repo gets its own project so per-repo policies (approvals, teams)
    # can layer on later. Project.slug is unique; we namespace with cifix_.
    async with get_db() as session:
        slug = f"cifix_{event.repo_full_name.replace('/', '__')}"
        result = await session.execute(select(Project).where(Project.slug == slug))
        project = result.scalar_one_or_none()
        if project is None:
            project = Project(
                name=f"CI Fixer · {event.repo_full_name}",
                slug=slug,
                repo_url=f"https://github.com/{event.repo_full_name}",
                repo_provider="github",
                default_branch="main",
                domain="ci_fix",
                onboarding_status="active",
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)

        # Normalized ci_context — cifix_commander stores this in WorkOrder.raw_command
        # as JSON, Tech Lead / Engineer read it from their Task.description.
        ci_context = {
            "repo": event.repo_full_name,
            "branch": event.branch,
            "sha": event.commit_sha,
            "pr_number": event.pr_number,
            "failing_job_id": event.build_id,
            "failing_job_name": (event.failed_jobs or [""])[0],
            # failing_command is intentionally omitted — Tech Lead derives it
            # from fetch_ci_log and writes it into fix_spec.failing_command.
            "ci_provider": event.provider,
            "build_url": event.build_url,
            "ci_fix_run_id": ci_run.id,  # back-reference for debugging
        }

        job_name = ci_context["failing_job_name"] or "CI failure"
        wo = WorkOrder(
            project_id=project.id,
            channel_id=None,  # webhook has no Slack channel binding
            title=f"Fix CI: {event.repo_full_name}#{event.pr_number} — {job_name}",
            description=(
                f"CI failure on {event.repo_full_name} PR #{event.pr_number}, "
                f"branch {event.branch}, job {job_name!r}. Dispatched by webhook."
            ),
            raw_command=json.dumps(ci_context),
            requested_by="ci_webhook",
            priority=60,
            status="OPEN",
            work_order_type="ci_fix",
        )
        session.add(wo)
        await session.commit()
        await session.refresh(wo)

        run_id = str(_uuid.uuid4())
        wo_id = wo.id
        project_id = project.id

    # Dispatch cifix_commander OUTSIDE the session (mirrors build-flow pattern —
    # keeps the DB connection free while Celery does its thing).
    router = TaskRouter(_celery)
    router.dispatch(
        agent_role="cifix_commander",
        task_id=wo_id,
        run_id=run_id,
        payload={"work_order_id": wo_id, "project_id": project_id},
    )

    log.info(
        "ci_webhook.v3_dispatched",
        ci_fix_run_id=ci_run.id,
        work_order_id=wo_id,
        run_id=run_id,
        project_id=project_id,
        repo=event.repo_full_name,
    )


async def _is_phalanx_fix_commit(repo_full_name: str, head_sha: str) -> bool:
    """Return True if head_sha matches a recent CIFixRun.fix_commit_sha for
    this repo — i.e., the CI was triggered by a commit Phalanx itself pushed.

    Bug #11 A1 mitigation. Looks at CIFixRuns from the last hour to keep
    the query bounded. Supports both v3's full 40-char fix_commit_sha and
    v1/v2's 8-char prefix via head_sha.startswith(stored).
    """
    async with get_db() as session:
        result = await session.execute(
            select(CIFixRun)
            .where(
                CIFixRun.repo_full_name == repo_full_name,
                CIFixRun.fix_commit_sha.isnot(None),
                CIFixRun.created_at >= datetime.now(UTC) - timedelta(hours=1),
            )
            .order_by(CIFixRun.created_at.desc())
            .limit(50)
        )
        for ci_run in result.scalars():
            if ci_run.fix_commit_sha and head_sha.startswith(ci_run.fix_commit_sha):
                return True
    return False


async def _update_fix_branch_ci_status(
    repo_full_name: str,
    branch: str,
    head_sha: str,
    conclusion: str,
) -> None:
    """
    When CI completes on a branch, check for an author_branch CIFixRun whose
    fix_commit_sha is a prefix of head_sha. If found, record the result and
    post a follow-up comment on the original PR.
    """
    status_map = {"success": "passed", "failure": "failed", "timed_out": "failed"}
    new_status = status_map.get(conclusion, "failed")

    async with get_db() as session:
        result = await session.execute(
            select(CIFixRun).where(
                CIFixRun.repo_full_name == repo_full_name,
                CIFixRun.branch == branch,
                CIFixRun.fix_strategy == "author_branch",
                CIFixRun.fix_branch_ci_status == "pending",
            )
        )
        ci_run = result.scalar_one_or_none()
        if ci_run is None:
            return

        # fix_commit_sha is stored as short sha (8 chars); head_sha is full 40-char
        if ci_run.fix_commit_sha and not head_sha.startswith(ci_run.fix_commit_sha):
            return

        await session.execute(
            update(CIFixRun).where(CIFixRun.id == ci_run.id).values(fix_branch_ci_status=new_status)
        )
        await session.commit()

    log.info(
        "ci_webhook.closed_loop_updated",
        ci_fix_run_id=ci_run.id,
        status=new_status,
        sha=head_sha[:12],
    )

    if ci_run.pr_number:
        await _post_closed_loop_comment(ci_run, new_status)


async def _post_closed_loop_comment(ci_run: CIFixRun, status: str) -> None:
    """Post a follow-up comment on the original PR with the CI result."""
    import httpx  # noqa: PLC0415

    async with get_db() as session:
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.id == ci_run.integration_id)
        )
        integration = result.scalar_one_or_none()

    if not integration or not integration.github_token:
        return

    if status == "passed":
        body = "✅ **Phalanx CI Fixer** — CI passed after the lint fix commit. Your PR is green."
    else:
        body = (
            f"⚠️ **Phalanx CI Fixer** — CI re-ran after the lint fix but is still **{status}**. "
            "The fix may have missed some errors, or a separate failure is now exposed. "
            f"*Fix run: `{ci_run.id}`*"
        )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.github.com/repos/{ci_run.repo_full_name}"
                f"/issues/{ci_run.pr_number}/comments",
                headers={
                    "Authorization": f"Bearer {integration.github_token}",
                    "Accept": "application/vnd.github+json",
                },
                json={"body": body},
            )
        log.info("ci_webhook.closed_loop_comment_posted", pr=ci_run.pr_number, status=status)
    except Exception as exc:
        log.warning("ci_webhook.closed_loop_comment_failed", error=str(exc))


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


@router.post("/github", status_code=status.HTTP_200_OK)
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

        # Bot-loop guard: skip check_runs triggered by Phalanx's own commits.
        # When Phalanx pushes a lint fix to the author's branch, GitHub fires
        # another check_run webhook — we must not re-process it.
        # NOTE: for GitHub App installs, sender.login will be "<app-name>[bot]";
        # set settings.git_author_name to match (e.g. "phalanx[bot]") if using an App.
        sender_login = payload.get("sender", {}).get("login", "")
        if sender_login.lower() == settings.git_author_name.lower():
            log.info(
                "ci_webhook.github.bot_commit_skipped",
                sender=sender_login,
                repo=payload.get("repository", {}).get("full_name"),
            )
            return {"status": "ignored", "reason": "bot_commit"}

        if action != "completed" or conclusion not in ("failure", "timed_out"):
            # Closed-loop: if CI passed/failed on a branch with a pending author_branch fix,
            # record the result even though we won't dispatch a new fix.
            if action == "completed" and conclusion in ("success", "failure", "timed_out"):
                check_run = payload.get("check_run", {})
                repo = payload.get("repository", {})
                head_branch = check_run.get("check_suite", {}).get("head_branch") or ""
                head_sha = check_run.get("head_sha", "")
                await _update_fix_branch_ci_status(
                    repo.get("full_name", ""), head_branch, head_sha, conclusion
                )
            return {"status": "ignored", "reason": f"action={action} conclusion={conclusion}"}

        check_run = payload["check_run"]
        repo = payload["repository"]

        # Bug #11 A1 — bot-loop guard via fix_commit_sha lookup.
        # When v3 commits a partial fix and pushes, the new SHA triggers a
        # fresh CI build whose failures fire fresh webhooks. Without this
        # check we'd dispatch a parallel v3 run for the same PR (the bug
        # surfaced 2026-04-28 on testbed PR #18). The existing sender.login
        # filter doesn't catch this because token-based pushes show up as
        # the PAT owner, not the git author. Matching head_sha against
        # CIFixRun.fix_commit_sha is the reliable signal.
        head_sha = check_run.get("head_sha", "")
        head_branch = check_run.get("check_suite", {}).get("head_branch") or ""
        if head_sha and await _is_phalanx_fix_commit(repo["full_name"], head_sha):
            log.info(
                "ci_webhook.skipping_own_fix_commit",
                repo=repo["full_name"],
                head_sha=head_sha[:12],
                conclusion=conclusion,
            )
            # Still record outcome on the closed-loop tracking — the original
            # CIFixRun wants to know its fix's CI verdict.
            await _update_fix_branch_ci_status(repo["full_name"], head_branch, head_sha, conclusion)
            return {"status": "ignored", "reason": "own_fix_commit"}

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

        # Bug #11 A3: extract GitHub's check_suite.id as the idempotency key.
        # Multiple check_runs of the same workflow run share this id; the
        # dispatcher uses it to dedup deterministically (vs the time-window
        # heuristic which has bypass edges — see bug #11 deep analysis).
        check_suite_id = check_run.get("check_suite", {}).get("id")

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
            ci_check_suite_id=check_suite_id,
            raw_payload=payload,
        )

        # Closed-loop: if this failure is on a branch where Phalanx already
        # pushed a lint fix, record that the fix didn't fully resolve the CI failure.
        await _update_fix_branch_ci_status(
            repo["full_name"],
            check_run["check_suite"]["head_branch"] or "",
            check_run["head_sha"],
            conclusion,
        )

        ci_run = await _dispatch_ci_fix(event)
        return {
            "status": "dispatched" if ci_run else "skipped",
            "ci_fix_run_id": ci_run.id if ci_run else None,
        }

    return {"status": "ignored", "event": x_github_event}


# ── Buildkite webhook ──────────────────────────────────────────────────────────


@router.post("/buildkite", status_code=status.HTTP_200_OK)
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


# ── CircleCI webhook ───────────────────────────────────────────────────────────


def _verify_circleci_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify CircleCI webhook signature.
    CircleCI sends: circleci-signature: v1=<hex_hmac_sha256>
    """
    if not secret:
        return True
    expected = "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@router.post("/circleci", status_code=status.HTTP_200_OK)
async def circleci_webhook(
    request: Request,
    circleci_signature: str = Header(default="", alias="circleci-signature"),
):
    """
    Receives CircleCI webhook events.

    Handles:
    - workflow-completed with status=failed → dispatch CI fix

    Setup in CircleCI: Project Settings → Webhooks
    Add URL: https://api.usephalanx.com/webhook/circleci
    Events: Workflow Completed
    Signing secret: set CIRCLECI_WEBHOOK_SECRET in phalanx env

    Payload shape (workflow-completed):
      {
        "type": "workflow-completed",
        "workflow": {
          "id": "<workflow_uuid>",
          "name": "<workflow_name>",
          "status": "failed",
          "created_at": "...",
          "stopped_at": "..."
        },
        "pipeline": {
          "id": "<pipeline_uuid>",
          "number": 42,
          "trigger": {"type": "webhook", ...},
          "vcs": {
            "origin_repository_url": "https://github.com/owner/repo",
            "branch": "fix/my-branch",
            "revision": "<commit_sha>",
            "commit": {"subject": "...", "author": {"login": "..."}}
          }
        },
        "project": {"id": "...", "name": "repo", "slug": "github/owner/repo"},
        "organization": {"name": "owner", ...}
      }
    """
    body = await request.body()

    if not _verify_circleci_signature(body, circleci_signature, settings.circleci_webhook_secret):
        log.warning("ci_webhook.circleci.invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid CircleCI signature")

    payload = json.loads(body)
    event_type = payload.get("type")

    if event_type != "workflow-completed":
        return {"status": "ignored", "type": event_type}

    workflow = payload.get("workflow", {})
    if workflow.get("status") not in ("failed", "error", "failing", "canceled"):
        return {"status": "ignored", "workflow_status": workflow.get("status")}

    pipeline = payload.get("pipeline", {})
    vcs = pipeline.get("vcs", {})

    # Extract repo name from the VCS URL (always GitHub for phalanx)
    repo_url = vcs.get("origin_repository_url", "")
    repo_full_name = _parse_repo_name(repo_url)
    if not repo_full_name:
        # Fallback: try project slug (format: "github/owner/repo")
        slug = payload.get("project", {}).get("slug", "")
        if slug.startswith("github/"):
            repo_full_name = slug[len("github/") :]
    if not repo_full_name:
        return {"status": "skipped", "reason": "cannot_parse_repo"}

    branch = vcs.get("branch", "")
    commit_sha = vcs.get("revision", "")
    pr_author: str | None = vcs.get("commit", {}).get("author", {}).get("login") or vcs.get(
        "commit", {}
    ).get("committer", {}).get("login")

    # CircleCI build_id = workflow ID (used to fetch job list + logs)
    workflow_id = workflow.get("id", "")
    workflow_name = workflow.get("name", "")
    build_url = (
        f"https://app.circleci.com/pipelines/github/{repo_full_name}"
        f"/{pipeline.get('number', '')}/workflows/{workflow_id}"
    )

    # PR number: CircleCI doesn't directly provide it in workflow webhooks.
    # It may be in the branch name (e.g. "pull/42") or absent.
    pr_number: int | None = None
    if branch.startswith("pull/"):
        import contextlib  # noqa: PLC0415

        with contextlib.suppress(IndexError, ValueError):
            pr_number = int(branch.split("/")[1])

    event = CIFailureEvent(
        provider="circleci",
        repo_full_name=repo_full_name,
        branch=branch,
        commit_sha=commit_sha,
        build_id=workflow_id,
        build_url=build_url,
        failed_jobs=[workflow_name] if workflow_name else [],
        pr_number=pr_number,
        pr_author=pr_author,
        raw_payload=payload,
    )

    ci_run = await _dispatch_ci_fix(event)
    return {
        "status": "dispatched" if ci_run else "skipped",
        "ci_fix_run_id": ci_run.id if ci_run else None,
    }


# ── Jenkins webhook (Phase 2 stub) ─────────────────────────────────────────────


@router.post("/jenkins", status_code=status.HTTP_200_OK)
async def jenkins_webhook(request: Request):
    """Jenkins webhook — Phase 2."""
    return {"status": "coming_soon", "provider": "jenkins"}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _parse_repo_name(repo_url: str) -> str | None:
    """Extract 'owner/repo' from a git remote URL."""
    import re  # noqa: PLC0415

    # https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    if m:
        return m.group(1)
    return None
