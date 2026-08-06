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


def _classify_phase_from_command(cmd: str | None) -> str:
    """P1-6 v2 — derive phase from the failing command. Order matters:
    `uv pip install` must classify as 'uv' BEFORE 'pip' because the
    substring 'pip install' also matches uv-driven installs."""
    if not isinstance(cmd, str):
        return "unknown"
    c = cmd.lower().strip()
    # uv ahead of pip — uv pip install, uv sync, uv run, uv add, uv venv
    if "uv pip" in c or c.startswith("uv ") or " uv " in c or "uv sync" in c or "uv run" in c or "uv venv" in c or "uv add" in c:
        return "uv"
    if "apt-get" in c or c.startswith("apt ") or "apt install" in c:
        return "apt"
    if "pip install" in c or c.startswith("pip ") or "pip3 install" in c:
        return "pip"
    if "git clone" in c or "git submodule" in c or c.startswith("git "):
        return "git"
    # Pre-install infra steps: docker run, mkdir, chown, docker cp, etc.
    if "docker run" in c or "docker cp" in c or c.startswith("mkdir") or c.startswith("chown"):
        return "infra"
    return "unknown"


def _classify_phase_from_error(error: str | None) -> str:
    """When no failed command is available (e.g. docker_run_failed
    happens before any command runs), the error string's prefix names
    the implicit step."""
    if not isinstance(error, str):
        return "unknown"
    head = error.lower()
    if head.startswith("docker_run_failed") or head.startswith("docker_cp") or head.startswith("mkdir_workspace"):
        return "infra"
    if head.startswith("baseline_apt_install") or "apt-get" in head:
        return "apt"
    if "pip install" in head:
        return "pip"
    if head.startswith("install_command_failed"):
        # Parse the command after the prefix and reclassify.
        return _classify_phase_from_command(error.split(":", 1)[-1].strip())
    return "unknown"


def _detect_sre_setup_failure(tasks: list) -> dict | None:
    """If the SRE setup task FAILED with sandbox_provisioning_failed,
    return a structured diagnostic dict; otherwise None.

    P1-6: previously returned bool. P1-6 v2 enriches the dict with
    per-command exit_code + stderr_tail + stdout_tail + phase + subclass,
    so the ledger row alone tells the operator exactly which command
    failed, with what exit code, and which install phase (apt/pip/uv/git/
    infra).

    Returned dict shape:
      {
        "failed_step":      pipeline step name (e.g. "docker_run", "apt_install_baseline", "install_command")
        "failed_command":   the exact command that failed (or implied step name)
        "exit_code":        int or None
        "error_message":    short stripped error string (max 500 chars)
        "stderr_tail":      last 500 chars of stderr from the failing step
        "stdout_tail":      last 500 chars of stdout from the failing step (or None)
        "phase":            one of: "apt", "pip", "uv", "git", "infra", "unknown"
        "failure_subclass": one of FAILED_SANDBOX_SETUP_{APT,PIP,UV,GIT,UNKNOWN}
        "base_image":       env_spec.base_image
        "stack":             env_spec.stack
        "install_commands": first 3 install_commands from env_spec (planned)
        "setup_log_tail":   last 3 entries from setup_log (full per-step records)
        "sre_task_id":      the source task's id (cross-reference)
      }
    """
    if not tasks:
        return None
    for t in tasks:
        role = getattr(t, "agent_role", None)
        if role not in {"cifix_sre_setup", "cifix_sre"}:
            continue
        status = getattr(t, "status", None)
        if status != "FAILED":
            continue
        err = getattr(t, "error", None) or ""
        if not (isinstance(err, str) and "sandbox_provisioning_failed" in err):
            continue

        # Strip the "sandbox_provisioning_failed: " prefix.
        error_message = err.split("sandbox_provisioning_failed:", 1)[-1].strip()

        # Pull structured fields out of task.output.
        out = getattr(t, "output", None) or {}
        out = out if isinstance(out, dict) else {}
        env_spec = out.get("env_spec") if isinstance(out.get("env_spec"), dict) else {}
        setup_log = out.get("setup_log") if isinstance(out.get("setup_log"), list) else []
        install_cmds = env_spec.get("install_commands") or []

        # The LAST entry in setup_log is the one that failed (provisioner
        # appends-then-fails). If setup_log is empty, the failure happened
        # before any step ran (typically docker_run_failed).
        last_step: dict = setup_log[-1] if setup_log else {}
        if not isinstance(last_step, dict):
            last_step = {}

        # Failure happens BEFORE any step is logged → derive everything
        # from the error prefix.
        if not setup_log:
            # error_message looks like "docker_run_failed: <reason>"
            prefix = error_message.split(":", 1)[0] if ":" in error_message else ""
            failed_step = prefix or "unknown"
            failed_command = {
                "docker_run_failed": "docker run",
            }.get(prefix, prefix or "unknown")
            exit_code = None
            stderr_tail = error_message.split(":", 1)[-1].strip() if ":" in error_message else error_message
            stdout_tail = None
            phase = _classify_phase_from_error(error_message)
        else:
            failed_step = last_step.get("step") or "unknown"
            failed_command = last_step.get("cmd") or failed_step
            exit_code = last_step.get("exit_code")
            stderr_tail = last_step.get("error") or error_message
            stdout_tail = last_step.get("stdout_tail")
            phase = _classify_phase_from_command(failed_command)
            if phase == "unknown":
                # Fall back to error-string classification (covers docker_cp etc.)
                phase = _classify_phase_from_error(error_message)

        from phalanx.runtime.infra_verdicts import SANDBOX_PHASE_TO_FAILURE_CLASS  # noqa: PLC0415
        failure_subclass = SANDBOX_PHASE_TO_FAILURE_CLASS.get(
            phase, SANDBOX_PHASE_TO_FAILURE_CLASS["unknown"]
        )

        return {
            "failed_step": failed_step,
            "failed_command": failed_command,
            "exit_code": exit_code,
            "error_message": (error_message or "")[:500],
            "stderr_tail": (stderr_tail or "")[-500:] if stderr_tail else None,
            "stdout_tail": (stdout_tail or "")[-500:] if stdout_tail else None,
            "phase": phase,
            "failure_subclass": failure_subclass,
            "base_image": env_spec.get("base_image"),
            "stack": env_spec.get("stack"),
            "install_commands": install_cmds[:3] if isinstance(install_cmds, list) else [],
            "setup_log_tail": setup_log[-3:],
            "sre_task_id": getattr(t, "id", None),
        }
    return None


def _classify_verdict(
    *, run_status: str, tl: dict, eng: dict, tasks: list | None = None,
) -> tuple[str, str | None]:
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
      - FAILED: anything else. Subdivided by `failure_class` on the
        ledger row (separately resolved by _resolve_failure_class):
          - FAILED_SANDBOX_SETUP: SRE setup couldn't provision the
            container (added v1.7.3 post-Phase-2a — surfaced E2 + E5)
          - FAILED_INFRA_TIMEOUT / FAILED_INFRA_WORKER_HANG: written
            by the v1.7.3 commander watchdog + stuck-task detector
          - NULL: genuine architecture/code failure beyond infra

    v1.7.3 post-Phase-2a addition: when `tasks` is supplied AND the
    SRE setup task FAILED with sandbox_provisioning_failed, classify as
    FAILED. Without this branch, sandbox-setup failures with no TL
    output were mis-classified as SAFE_ESCALATE via the
    confidence==0.0 fallback below.
    """
    if eng.get("shadow_mode") is True and eng.get("shadow_verdict") == "SHIPPED_PROPOSED":
        return "SHIPPED_PROPOSED", "engineer_shipped_proposed"

    # NEW v1.7.3 — infra failure detected from task evidence.
    # SRE-setup failure means TL never ran, so the empty-TL check
    # below would otherwise default to SAFE_ESCALATE.
    if _detect_sre_setup_failure(tasks or []):
        return "FAILED", "sandbox_setup_failed"

    if isinstance(tl, dict):
        error_class = tl.get("error_class")
        validation_error = tl.get("validation_error") or ""
        # SAFE_ESCALATE (c) — calibration validator refused to ship a
        # hedged confidence on a clear-shape fix.
        if error_class == "plan_validation_failed" and isinstance(validation_error, str) and "confidence_calibration_failed" in validation_error:
            return "SAFE_ESCALATE", "calibration_failed"
        # SAFE_ESCALATE (d) — TL self-critique gate rejected its own
        # emit. v1.6.0 self_critique_inconsistent fires when TL emits
        # at confidence > 0.5 but flags one of the c1-c8 checks as
        # False (e.g., grounding_satisfied=False).
        if error_class == "self_critique_inconsistent":
            return "SAFE_ESCALATE", "self_critique_inconsistent"

    confidence = float(tl.get("confidence") or 0.0) if isinstance(tl, dict) else 0.0
    review_decision = tl.get("review_decision") if isinstance(tl, dict) else None
    if review_decision == "ESCALATE":
        return "SAFE_ESCALATE", "tl_escalated"
    if confidence == 0.0:
        return "SAFE_ESCALATE", "tl_zero_confidence"
    return "FAILED", "tl_emitted_but_engineer_did_not_ship"


async def _resolve_failure_class(
    run_id: str, tasks: list,
) -> tuple[str | None, dict | None]:
    """Resolve the ledger row's failure_class + diagnostic. Read order:
      1. runs.failure_class (set by v1.7.3 hardening commander/detector
         for FAILED_INFRA_TIMEOUT / FAILED_INFRA_WORKER_HANG).
      2. SRE setup task evidence → FAILED_SANDBOX_SETUP (with diagnostic).
      3. None (architecture failure or healthy run).

    Returns (failure_class, sre_setup_diagnostic).  The diagnostic is
    None unless failure_class == FAILED_SANDBOX_SETUP and the SRE setup
    task left a structured failure payload (P1-6).
    """
    async with get_db() as session:
        result = await session.execute(
            select(Run.failure_class).where(Run.id == run_id)
        )
        row = result.one_or_none()
    fc_from_run = row[0] if row else None

    # Always probe the SRE diagnostic — it's useful even when the Run row
    # already carries FAILED_SANDBOX_SETUP from a prior reconciliation.
    sre_diag = _detect_sre_setup_failure(tasks)

    if fc_from_run:
        # If Run.failure_class is already FAILED_SANDBOX_SETUP-shaped and
        # we have a fresh diagnostic, prefer the diagnostic's specific
        # subclass over the generic class. Otherwise pass through.
        from phalanx.runtime.infra_verdicts import (  # noqa: PLC0415
            FAILED_SANDBOX_SETUP,
            INFRA_FAILURE_CLASSES,
        )
        is_sandbox_class = (
            fc_from_run == FAILED_SANDBOX_SETUP
            or fc_from_run.startswith("FAILED_SANDBOX_SETUP_")
        )
        if is_sandbox_class and sre_diag is not None:
            # Promote to the specific subclass from the diagnostic.
            return sre_diag["failure_subclass"], sre_diag
        return fc_from_run, (sre_diag if is_sandbox_class else None)

    if sre_diag is not None:
        # New-row write path: subclass from diagnostic.
        return sre_diag["failure_subclass"], sre_diag

    return None, None


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
        await session.refresh(row)
        # P0-2 — JSONL export AFTER commit. Failure logged, never raised.
        from phalanx.shadow.ledger_export import append_ledger_row_async
        await append_ledger_row_async(
            ledger_crud.to_dict(row), exported_by="link_run_id"
        )

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

    # P0-6 — if the run is still non-terminal at CLI poll timeout, do NOT
    # write a terminal verdict. Leave the row PENDING so the reconciler
    # can finalize it from the run's actual terminal state when it lands.
    # This closes the 2026-05-12 W2 Batch 1 evidence-corruption hole.
    _NON_TERMINAL_RUN_STATUSES = {"INTAKE", "EXECUTING", "PENDING", "PLANNING"}
    if final_status in _NON_TERMINAL_RUN_STATUSES or final_status == "TIMEOUT":
        log.info(
            "shadow.cli_pending_exit",
            run_id=run_id,
            ledger_id=ledger_id,
            run_status=final_status,
            elapsed_s=elapsed_s,
            reason="run did not reach terminal state within CLI poll budget; "
                   "reconciler will finalize",
        )
        async with get_db() as session:
            row = await ledger_crud.get(session, ledger_id)
        return {
            **ledger_crud.to_dict(row),
            "_cli_status": "RUN_STILL_ACTIVE",
            "_cli_message": (
                f"Run did not terminate within {poll_timeout_s}s. "
                f"Ledger row left PENDING; reconciler will finalize within "
                f"~2 minutes of run completion. "
                f"Track with: phalanx shadow show {ledger_id}"
            ),
        }

    evidence = await _read_terminal_evidence(run_id)
    tl = evidence["tl_output"]
    eng = evidence["engineer_output"]
    tasks = evidence["tasks"]
    verdict, classification_reason = _classify_verdict(
        run_status=final_status, tl=tl, eng=eng, tasks=tasks,
    )
    failure_class, sre_setup_diagnostic = await _resolve_failure_class(run_id, tasks)

    proposed_patch = eng.get("diff") if isinstance(eng, dict) else None
    confidence = (
        float(tl.get("confidence") or 0.0) if isinstance(tl, dict) and tl.get("confidence") is not None else None
    )
    root_cause = tl.get("root_cause") if isinstance(tl, dict) else None
    affected_files = tl.get("affected_files") if isinstance(tl, dict) else None
    tool_calls = tl.get("tool_calls_used") if isinstance(tl, dict) else None
    iterations = 1  # MVP — multi-iter is a separate workstream

    # P0-5 — synthesize root_cause if SAFE_ESCALATE landed with no diagnosis.
    # Operationally a SAFE_ESCALATE row with NULL root_cause is
    # indistinguishable from a silent failure — closes W1.10 blind spot.
    # P1-6 — also synthesize root_cause for FAILED + FAILED_SANDBOX_SETUP
    # from the structured SRE diagnostic so the ledger row alone tells
    # the operator which step failed and why.
    root_cause_synthesized = False
    root_cause_synthesis_reason: str | None = None
    if verdict == "SAFE_ESCALATE" and not (root_cause or "").strip():
        from phalanx.shadow.provenance import synthesize_root_cause_for_safe_escalate  # noqa: PLC0415
        root_cause = synthesize_root_cause_for_safe_escalate(
            classification_reason, tl if isinstance(tl, dict) else {}
        )
        root_cause_synthesized = True
        root_cause_synthesis_reason = classification_reason or "unspecified"
    elif (
        verdict == "FAILED"
        and not (root_cause or "").strip()
        and sre_setup_diagnostic is not None
    ):
        from phalanx.shadow.provenance import synthesize_root_cause_for_sandbox_setup  # noqa: PLC0415
        root_cause = synthesize_root_cause_for_sandbox_setup(sre_setup_diagnostic)
        root_cause_synthesized = True
        root_cause_synthesis_reason = "sandbox_setup_failed"

    # P0-5 — build provenance + run consistency check before writing.
    from phalanx.shadow.provenance import (  # noqa: PLC0415
        build_provenance,
        check_consistency,
    )
    provenance = build_provenance(
        tasks,
        root_cause_synthesized=root_cause_synthesized,
        root_cause_synthesis_reason=root_cause_synthesis_reason,
        sre_setup_diagnostic=sre_setup_diagnostic,
    )
    tl_root_cause_full = tl.get("root_cause") if isinstance(tl, dict) else None
    divergence_detected, divergence_reasons = check_consistency(
        ledger_confidence=confidence,
        ledger_root_cause=root_cause,
        provenance=provenance,
        tl_task_root_cause_full=tl_root_cause_full,
    )
    if divergence_detected:
        provenance["divergence_detected"] = True
        provenance["divergence_details"] = divergence_reasons
        log.error(
            "ledger.divergence",
            run_id=run_id,
            ledger_id=ledger_id,
            reasons=divergence_reasons,
            provenance=provenance,
        )

    # P0-6 — terminal-state validator. Refuse to write an ill-formed
    # snapshot (e.g. SAFE_ESCALATE with TL task still PENDING). The
    # reconciler will finalize the row when the run actually terminates.
    from phalanx.shadow.provenance import is_well_formed_terminal_state  # noqa: PLC0415
    ok, refusal_reason = is_well_formed_terminal_state(
        verdict=verdict, provenance=provenance,
    )
    if not ok:
        log.warning(
            "shadow.cli.refusing_ill_formed_terminal",
            run_id=run_id,
            ledger_id=ledger_id,
            verdict=verdict,
            reason=refusal_reason,
        )
        async with get_db() as session:
            row = await ledger_crud.get(session, ledger_id)
        return {
            **ledger_crud.to_dict(row),
            "_cli_status": "ILL_FORMED_SNAPSHOT_REFUSED",
            "_cli_message": (
                f"Refused to write ill-formed terminal snapshot: {refusal_reason}. "
                f"Ledger row left PENDING; reconciler will finalize. "
                f"Track with: phalanx shadow show {ledger_id}"
            ),
        }

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
            failure_class=failure_class,
            notes=(
                f"run_status={final_status}; "
                f"tl_review_decision={tl.get('review_decision') if isinstance(tl, dict) else None}"
            ),
            provenance=provenance,
        )
        return ledger_crud.to_dict(updated)
