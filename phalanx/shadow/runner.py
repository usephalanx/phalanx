"""Shadow runner — orchestrates one shadow-mode dispatch.

Given (repo, workflow_run_id):
  1. Fetch the workflow run + jobs from GitHub API to construct a
     CIFailureEvent equivalent.
  2. Look up the CIIntegration row for the repo (must exist with
     cifixer_version='v3'; the runner does not register repos).
  3. Create CIFixRun (dedup marker), Project (lazy), WorkOrder, Run
     with `shadow_mode=True`. Insert ledger row in PENDING state.
  4. Dispatch cifix_commander via the existing TaskRouter.
  5. Poll runs.status until terminal (SHIPPED / FAILED / ESCALATED).
  6. Read tasks (TL output, engineer output) to derive the verdict
     classification + fields. Update the ledger row.

The engineer's shadow short-circuit (cifix_engineer.py) ensures no
push or PR is opened — its task output carries the unified diff only.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select

from phalanx.db.models import (
    CIFixRun,
    CIIntegration,
    Project,
    Run,
    Task,
    WorkOrder,
)
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app
from phalanx.runtime.task_router import TaskRouter
from phalanx.shadow import ledger as ledger_crud

log = structlog.get_logger(__name__)

_TERMINAL_STATUSES = {"SHIPPED", "FAILED", "CANCELLED"}
_DEFAULT_POLL_INTERVAL_S = 10
_DEFAULT_POLL_TIMEOUT_S = 1800  # 30 min cap per shadow run


class ShadowRunnerError(Exception):
    """Raised when prereqs aren't met (no integration, GH API failure, etc.)."""


# ── GitHub API helpers ────────────────────────────────────────────────────


async def _gh_get(url: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        r.raise_for_status()
        return r.json()


async def _fetch_workflow_run_event(
    repo: str, workflow_run_id: int, token: str
) -> dict[str, Any]:
    """Return a dict shaped like CIFailureEvent fields needed by dispatch."""
    base = f"https://api.github.com/repos/{repo}"
    wf = await _gh_get(f"{base}/actions/runs/{workflow_run_id}", token)

    if wf.get("conclusion") not in {"failure", "cancelled", "timed_out"}:
        raise ShadowRunnerError(
            f"workflow run {workflow_run_id} concluded {wf.get('conclusion')!r}; "
            "shadow mode only runs on failed CI"
        )

    head_sha = wf.get("head_sha") or ""
    head_branch = wf.get("head_branch") or ""

    pr_number: int | None = None
    prs = wf.get("pull_requests") or []
    if prs:
        pr_number = prs[0].get("number")
    if pr_number is None and head_branch:
        # Fallback 1: GHA's `pull_requests` field is only populated for
        # same-repo active PRs; closed/merged PRs and historical workflow
        # runs often return []. List PRs by head branch.
        try:
            pr_list = await _gh_get(
                f"{base}/pulls?head={repo.split('/')[0]}:{head_branch}&state=all&per_page=5",
                token,
            )
            if isinstance(pr_list, list) and pr_list:
                pr_number = pr_list[0].get("number")
        except Exception:  # noqa: BLE001
            pass
    if pr_number is None and head_sha:
        # Fallback 2: cross-fork PRs (contributor's branch lives on a
        # different user's repo) won't show up in branch-based search.
        # Use the search API by commit sha — works regardless of fork.
        try:
            search = await _gh_get(
                f"https://api.github.com/search/issues?q=repo:{repo}+sha:{head_sha}+type:pr",
                token,
            )
            items = (search or {}).get("items") or []
            if items:
                pr_number = items[0].get("number")
        except Exception:  # noqa: BLE001
            pass

    # Failed jobs
    jobs = await _gh_get(
        f"{base}/actions/runs/{workflow_run_id}/jobs?per_page=50", token
    )
    failed = [j for j in (jobs.get("jobs") or []) if j.get("conclusion") == "failure"]
    failed_job_names = [j["name"] for j in failed]
    failing_job_id = str(failed[0]["id"]) if failed else str(workflow_run_id)

    return {
        "repo_full_name": repo,
        "branch": head_branch,
        "commit_sha": head_sha,
        "pr_number": pr_number,
        "build_id": failing_job_id,
        "build_url": wf.get("html_url") or "",
        "failed_jobs": failed_job_names,
        "ci_check_suite_id": wf.get("check_suite_id"),
    }


# ── DB plumbing — mirrors _dispatch_ci_fix_v3 minus webhook bits ─────────


async def _resolve_or_create_project(session, repo: str) -> Project:
    slug = f"cifix_{repo.replace('/', '__')}"
    result = await session.execute(select(Project).where(Project.slug == slug))
    project = result.scalar_one_or_none()
    if project is not None:
        return project
    project = Project(
        name=f"CI Fixer · {repo}",
        slug=slug,
        repo_url=f"https://github.com/{repo}",
        repo_provider="github",
        default_branch="main",
        domain="ci_fix",
        onboarding_status="active",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _create_shadow_run_chain(
    *,
    repo: str,
    integration_id: str,
    event: dict[str, Any],
) -> tuple[str, str, str, dict]:
    """Insert CIFixRun + Project + WorkOrder + Run(shadow_mode=True).

    Returns (run_id, work_order_id, ci_fix_run_id, ci_context).
    """
    async with get_db() as session:
        # Shadow runs intentionally set ci_check_suite_id=None so they
        # bypass the `ci_fix_runs_repo_check_suite_idem` partial unique
        # index (which is `WHERE ci_check_suite_id IS NOT NULL`). This
        # lets us shadow workflows that were previously dispatched via
        # webhook without UNIQUE conflicts. Dedup for shadow runs lives
        # on `shadow_ledger UNIQUE (repo, workflow_run_id)` instead.
        ci_run = CIFixRun(
            integration_id=integration_id,
            repo_full_name=repo,
            branch=event["branch"],
            pr_number=event["pr_number"],
            commit_sha=event["commit_sha"],
            ci_provider="github_actions",
            ci_build_id=event["build_id"],
            ci_check_suite_id=None,
            build_url=event["build_url"],
            failed_jobs=event["failed_jobs"],
            failure_summary="(shadow mode — manual dispatch)",
            status="PENDING",
            attempt=1,
        )
        session.add(ci_run)
        await session.commit()
        await session.refresh(ci_run)

        project = await _resolve_or_create_project(session, repo)

        ci_context = {
            "repo": repo,
            "branch": event["branch"],
            "sha": event["commit_sha"],
            "pr_number": event["pr_number"],
            "failing_job_id": event["build_id"],
            "failing_job_name": (event["failed_jobs"] or [""])[0],
            "ci_provider": "github_actions",
            "build_url": event["build_url"],
            "ci_fix_run_id": ci_run.id,
            "shadow_mode": True,
        }

        job_name = ci_context["failing_job_name"] or "CI failure"
        wo = WorkOrder(
            project_id=project.id,
            channel_id=None,
            title=f"[SHADOW] Fix CI: {repo}#{event['pr_number']} — {job_name}",
            description=(
                f"Shadow-mode dispatch for {repo} workflow run {event['build_id']}. "
                "Engineer must NOT push; ledger captures verdict."
            ),
            raw_command=json.dumps(ci_context),
            requested_by="shadow_cli",
            priority=60,
            status="OPEN",
            work_order_type="ci_fix",
        )
        session.add(wo)
        await session.commit()
        await session.refresh(wo)

        run_id = str(uuid.uuid4())
        # Pre-create the Run row with shadow_mode=True so the commander
        # picks it up via the existing run_already_exists short-circuit.
        existing_count = await session.execute(
            select(func.count()).select_from(Run).where(Run.work_order_id == wo.id)
        )
        run = Run(
            id=run_id,
            work_order_id=wo.id,
            project_id=project.id,
            run_number=existing_count.scalar_one() + 1,
            status="INTAKE",
            shadow_mode=True,
        )
        session.add(run)
        await session.commit()

        return run_id, wo.id, ci_run.id, ci_context


# ── Polling + classification ─────────────────────────────────────────────


async def _wait_for_terminal(
    run_id: str,
    *,
    poll_interval_s: int = _DEFAULT_POLL_INTERVAL_S,
    timeout_s: int = _DEFAULT_POLL_TIMEOUT_S,
) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        async with get_db() as session:
            result = await session.execute(select(Run.status).where(Run.id == run_id))
            status = result.scalar_one_or_none()
        if status in _TERMINAL_STATUSES:
            return status
        await asyncio.sleep(poll_interval_s)
    return "TIMEOUT"


async def _read_terminal_evidence(run_id: str) -> dict[str, Any]:
    """Pull TL + engineer outputs and aggregate cost/time."""
    async with get_db() as session:
        result = await session.execute(
            select(Task)
            .where(Task.run_id == run_id)
            .order_by(Task.sequence_num.asc())
        )
        tasks = list(result.scalars().all())
    by_role: dict[str, Task] = {}
    for t in tasks:
        # Keep the LAST occurrence per role (iter-N wins)
        by_role[t.agent_role] = t

    tl = by_role.get("cifix_techlead")
    eng = by_role.get("cifix_engineer")

    tl_out = (tl.output if tl is not None else None) or {}
    eng_out = (eng.output if eng is not None else None) or {}

    total_tokens = sum((t.tokens_used or 0) for t in tasks)

    return {
        "tl_output": tl_out,
        "engineer_output": eng_out,
        "tasks": tasks,
        "total_tokens": total_tokens,
    }


def _classify_verdict(*, run_status: str, tl: dict, eng: dict) -> str:
    """SHIPPED_PROPOSED / SAFE_ESCALATE / FAILED.

    The classifier maps each terminal run state to one of three outcomes
    the ledger treats as meaningful:

      - SHIPPED_PROPOSED: engineer ran in shadow mode and emitted a
        verified proposed_patch.
      - SAFE_ESCALATE: TL or a validator refused to ship insufficient
        evidence. Four sub-cases:
          (a) TL emitted review_decision=\"ESCALATE\" (canonical).
          (b) TL confidence==0.0 (canonical low-confidence escalate).
          (c) v1.7.2.9 calibration validator rejected a hedged
              confidence on a localized deterministic fix.
          (d) v1.6.0 self_critique gate rejected an emit because TL
              flagged one of its own c1-c8 checks (e.g.,
              grounding_satisfied) as False — TL caught its own
              evidence gap and refused to ship.
        All four sub-cases share the same semantic property: the
        architecture declined to ship rather than guessed.
      - FAILED: anything else (genuine pipeline failure, sandbox crash,
        Celery hang). Note: runtime hardening also writes
        runs.failure_class for infra-specific reasons
        (FAILED_INFRA_TIMEOUT / FAILED_INFRA_WORKER_HANG / etc.); the
        runner records that on the ledger row separately so aggregate
        metrics can split signal from infra noise.
    """
    if eng.get("shadow_mode") is True and eng.get("shadow_verdict") == "SHIPPED_PROPOSED":
        return "SHIPPED_PROPOSED"

    if isinstance(tl, dict):
        error_class = tl.get("error_class")
        validation_error = tl.get("validation_error") or ""
        # SAFE_ESCALATE (c) — calibration validator refused to ship a
        # hedged confidence on a clear-shape fix.
        if error_class == "plan_validation_failed" and isinstance(validation_error, str) and "confidence_calibration_failed" in validation_error:
            return "SAFE_ESCALATE"
        # SAFE_ESCALATE (d) — TL self-critique gate rejected its own
        # emit. v1.6.0 self_critique_inconsistent fires when TL emits
        # at confidence > 0.5 but flags one of the c1-c8 checks as
        # False (e.g., grounding_satisfied=False). The architecture
        # caught its own grounding gap and refused to ship — same
        # semantic property as the calibration validator above.
        # Surfaced concretely on the v1.7.3 hardening proof S4 run:
        # TL emitted at 0.76 with grounding_satisfied=False; the gate
        # rejected; ledger landed FAILED, masking the safety win.
        if error_class == "self_critique_inconsistent":
            return "SAFE_ESCALATE"

    confidence = float(tl.get("confidence") or 0.0) if isinstance(tl, dict) else 0.0
    review_decision = tl.get("review_decision") if isinstance(tl, dict) else None
    if review_decision == "ESCALATE" or confidence == 0.0:
        return "SAFE_ESCALATE"
    return "FAILED"


def _approx_cost_usd(total_tokens: int) -> float:
    """Coarse cost estimate. Real cost lives in CostRecord on each agent
    but for the MVP we approximate from total tokens at $5/1M (mix of
    GPT-5.4 reasoning + Sonnet)."""
    return round(total_tokens * 5.0 / 1_000_000, 4)


# ── Public entry point ───────────────────────────────────────────────────


async def run_shadow_for_workflow(
    *,
    repo: str,
    workflow_run_id: int,
    poll_interval_s: int = _DEFAULT_POLL_INTERVAL_S,
    poll_timeout_s: int = _DEFAULT_POLL_TIMEOUT_S,
) -> dict[str, Any]:
    """End-to-end: fetch event → dispatch shadow run → wait → write ledger.

    Returns the JSON-serializable ledger row.
    """
    started_at = time.time()

    async with get_db() as session:
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.repo_full_name == repo)
        )
        integration = result.scalar_one_or_none()
    if integration is None or not integration.github_token:
        raise ShadowRunnerError(
            f"no CIIntegration row with github_token for {repo!r}. "
            "Register the repo first (existing webhook setup flow)."
        )
    if integration.cifixer_version != "v3":
        raise ShadowRunnerError(
            f"{repo} is on cifixer_version={integration.cifixer_version!r}; "
            "shadow runner requires v3."
        )

    log.info("shadow.fetch_event.start", repo=repo, workflow_run_id=workflow_run_id)
    event = await _fetch_workflow_run_event(repo, workflow_run_id, integration.github_token)
    log.info(
        "shadow.fetch_event.done",
        repo=repo,
        commit_sha=event["commit_sha"][:8],
        pr_number=event["pr_number"],
        failed_jobs=event["failed_jobs"],
    )

    # Pre-create ledger row (PENDING) so we have an id even on failure.
    async with get_db() as session:
        ledger_row = await ledger_crud.create_pending(
            session,
            repo=repo,
            workflow_run_id=workflow_run_id,
            pr_number=event["pr_number"],
            failing_commit_sha=event["commit_sha"],
        )
        ledger_id = ledger_row.id

    run_id, wo_id, ci_fix_run_id, ci_context = await _create_shadow_run_chain(
        repo=repo, integration_id=integration.id, event=event
    )

    # Link the Run id back to the ledger row.
    async with get_db() as session:
        row = await ledger_crud.get(session, ledger_id)
        row.phalanx_run_id = run_id
        await session.commit()

    log.info(
        "shadow.dispatch",
        repo=repo,
        run_id=run_id,
        work_order_id=wo_id,
        ledger_id=ledger_id,
    )

    router = TaskRouter(celery_app)
    router.dispatch(
        agent_role="cifix_commander",
        task_id=wo_id,
        run_id=run_id,
        payload={"work_order_id": wo_id, "project_id": ci_context.get("project_id")},
    )

    final_status = await _wait_for_terminal(
        run_id, poll_interval_s=poll_interval_s, timeout_s=poll_timeout_s
    )
    elapsed_s = int(time.time() - started_at)
    log.info("shadow.terminal", run_id=run_id, status=final_status, elapsed_s=elapsed_s)

    evidence = await _read_terminal_evidence(run_id)
    tl = evidence["tl_output"]
    eng = evidence["engineer_output"]
    verdict = _classify_verdict(run_status=final_status, tl=tl, eng=eng)

    proposed_patch = eng.get("diff") if isinstance(eng, dict) else None
    confidence = (
        float(tl.get("confidence") or 0.0) if isinstance(tl, dict) and tl.get("confidence") is not None else None
    )
    root_cause = tl.get("root_cause") if isinstance(tl, dict) else None
    affected_files = tl.get("affected_files") if isinstance(tl, dict) else None
    tool_calls = tl.get("tool_calls_used") if isinstance(tl, dict) else None
    iterations = 1  # MVP — multi-iter is a separate workstream

    async with get_db() as session:
        updated = await ledger_crud.update_with_results(
            session,
            ledger_id=ledger_id,
            verdict=verdict,
            confidence=confidence,
            proposed_patch=proposed_patch,
            root_cause=root_cause,
            affected_files=affected_files if isinstance(affected_files, list) else None,
            iterations=iterations,
            tool_calls=tool_calls,
            cost_usd=_approx_cost_usd(evidence["total_tokens"]),
            run_seconds=elapsed_s,
            notes=(
                f"run_status={final_status}; "
                f"tl_review_decision={tl.get('review_decision') if isinstance(tl, dict) else None}"
            ),
        )
        return ledger_crud.to_dict(updated)
