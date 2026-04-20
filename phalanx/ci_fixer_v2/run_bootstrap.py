"""Run bootstrap — one entry point per webhook-triggered CI fix run.

Responsibilities:
  1. Load the CIFixRun + CIIntegration rows from the DB.
  2. Clone the repo workspace at the failing commit (via v1 helpers).
  3. Provision sandbox (via v1 SandboxProvisioner).
  4. Build AgentContext with api_key, sandbox_container_id, workspace_path,
     fingerprint_hash, pr_number, has_write_permission, author_head_branch.
  5. Wire the Sonnet coder seam to the real Sonnet callable.
  6. Run the main agent loop with the GPT reasoning callable.
  7. Finalize cost breakdown + persist run outcome (status,
     cost_breakdown_json, fix_commit_sha, fix_branch, fix_strategy, error).

Everything that reaches outside this module (DB, v1 helpers, providers)
is behind a named module-level seam so the bootstrap itself is unit-
testable end-to-end without docker, git, or network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from phalanx.ci_fixer_v2.agent import RunOutcome, run_ci_fix_v2
from phalanx.ci_fixer_v2.config import RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.pricing import finalize_cost_record
from phalanx.ci_fixer_v2.prompts import (
    CODER_SUBAGENT_SYSTEM_PROMPT,
    MAIN_AGENT_SYSTEM_PROMPT,
)
from phalanx.ci_fixer_v2.tool_scopes import (
    coder_subagent_tool_schemas,
    main_agent_tool_schemas,
)

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Test seams — replace with no-ops / canned values in bootstrap tests
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BootstrapInputs:
    """Everything the bootstrap needs to run that comes from DB + settings.

    Loaded by `_load_run_inputs` in production; tests build one directly.
    """

    ci_fix_run_id: str
    repo_full_name: str
    ci_provider: str
    fingerprint_hash: str | None
    pr_number: int | None
    branch: str  # author's PR head branch (becomes author_head_branch on ctx)
    original_failing_command: str
    # CI context needed to seed the agent's initial user message —
    # without these the agent starts with empty context and can't
    # meaningfully act on turn 1.
    ci_build_id: str
    commit_sha: str
    build_url: str
    failed_jobs: list[str]
    # External identities
    github_token: str
    openai_api_key: str
    anthropic_api_key: str
    # Model names
    openai_model: str
    anthropic_model: str
    # Permission flag (resolved from integration config + author filter)
    has_write_permission: bool


async def _load_run_inputs(ci_fix_run_id: str) -> BootstrapInputs:
    """Read CIFixRun + CIIntegration and assemble a BootstrapInputs."""
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select

    from phalanx.config.settings import get_settings
    from phalanx.db.models import CIFixRun, CIIntegration
    from phalanx.db.session import get_db

    async with get_db() as session:
        result = await session.execute(
            select(CIFixRun, CIIntegration)
            .join(
                CIIntegration, CIIntegration.id == CIFixRun.integration_id
            )
            .where(CIFixRun.id == ci_fix_run_id)
        )
        row = result.first()
        if row is None:
            raise RuntimeError(f"ci_fix_run_not_found: {ci_fix_run_id}")
        run, integ = row

    settings = get_settings()
    # Prefer the per-integration token; fall back to the global github_token.
    gh_token = integ.github_token or settings.github_token

    return BootstrapInputs(
        ci_fix_run_id=str(run.id),
        repo_full_name=run.repo_full_name,
        ci_provider=run.ci_provider,
        fingerprint_hash=run.fingerprint_hash,
        pr_number=run.pr_number,
        branch=run.branch,
        original_failing_command=run.failure_summary or "",
        ci_build_id=run.ci_build_id or "",
        commit_sha=run.commit_sha or "",
        build_url=run.build_url or "",
        failed_jobs=list(run.failed_jobs or []),
        github_token=gh_token,
        openai_api_key=settings.openai_api_key,
        anthropic_api_key=settings.anthropic_api_key,
        openai_model=settings.openai_model_reasoning_ci_fixer,
        anthropic_model=settings.anthropic_model_ci_fixer_coder,
        has_write_permission=integ.auto_commit,  # approximation for MVP
    )


def _build_initial_user_message(inputs: BootstrapInputs) -> dict[str, object]:
    """Seed message the agent sees on turn 1.

    Without this, ctx.messages is [] and the agent's first LLM call
    hits an empty conversation — the model has no idea what to do.
    This message carries exactly the context the system prompt tells
    the agent to use (fetch_ci_log with a job_id, query_fingerprint,
    etc.).
    """
    failed_jobs_display = ", ".join(inputs.failed_jobs) if inputs.failed_jobs else "(not recorded)"
    sha_short = inputs.commit_sha[:7] if inputs.commit_sha else "unknown"
    content = f"""A CI failure has occurred on an open pull request. Diagnose and close the PR.

Repository:        {inputs.repo_full_name}
PR number:         {inputs.pr_number if inputs.pr_number is not None else "(unknown)"}
Head branch:       {inputs.branch}
Failing commit:    {sha_short}
CI provider:       {inputs.ci_provider}
Failing job ID:    {inputs.ci_build_id}  ← pass this as job_id to fetch_ci_log
Failed jobs:       {failed_jobs_display}
Build URL:         {inputs.build_url or "(not provided)"}
Failing command:   {inputs.original_failing_command or "(inferred from log)"}

Start by calling fetch_ci_log with job_id="{inputs.ci_build_id}" to see the failure. Then follow your standard diagnose → decide → act → verify → coordinate workflow. Remember: close the PR or escalate cleanly — never commit without sandbox verification."""
    return {"role": "user", "content": content}


async def _clone_workspace(
    ci_fix_run_id: str, repo_full_name: str, branch: str, github_token: str
) -> str:
    """Clone the repo at the given branch; return absolute workspace path.
    Production path uses GitPython; tests stub this."""
    from pathlib import Path

    import git

    from phalanx.config.settings import get_settings

    base = Path(get_settings().git_workspace) / f"v2-{ci_fix_run_id}"
    if base.exists():
        import shutil

        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)

    url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    git.Repo.clone_from(url, base, branch=branch, depth=1)
    return str(base)


async def _provision_sandbox(workspace_path: str) -> str | None:
    """Provision a sandbox via the v1 provisioner; return container_id
    or None if sandbox is disabled/unavailable."""
    from pathlib import Path

    from phalanx.ci_fixer.sandbox import SandboxProvisioner

    provisioner = SandboxProvisioner()
    sandbox = await provisioner.provision(Path(workspace_path))
    if sandbox is None or not sandbox.available:
        return None
    return sandbox.container_id or None


def _build_main_llm(inputs: BootstrapInputs):
    from phalanx.ci_fixer_v2.providers import build_gpt_reasoning_callable

    return build_gpt_reasoning_callable(
        model=inputs.openai_model,
        api_key=inputs.openai_api_key,
        system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
        tool_schemas=main_agent_tool_schemas(),
    )


def _build_sonnet_llm(inputs: BootstrapInputs):
    from phalanx.ci_fixer_v2.providers import build_sonnet_coder_callable

    return build_sonnet_coder_callable(
        model=inputs.anthropic_model,
        api_key=inputs.anthropic_api_key,
        system_prompt=CODER_SUBAGENT_SYSTEM_PROMPT,
        tool_schemas=coder_subagent_tool_schemas(),
    )


async def _persist_run_outcome(
    ci_fix_run_id: str, ctx: AgentContext, outcome: RunOutcome
) -> None:
    """Write status + cost + fix metadata onto CIFixRun."""
    from sqlalchemy import select

    from phalanx.db.models import CIFixRun
    from phalanx.db.session import get_db

    async with get_db() as session:
        result = await session.execute(
            select(CIFixRun).where(CIFixRun.id == ci_fix_run_id)
        )
        run = result.scalar_one_or_none()
        if run is None:
            log.error("v2.bootstrap.persist_miss", ci_fix_run_id=ci_fix_run_id)
            return

        run.status = _map_verdict_to_status(outcome.verdict)
        run.cost_breakdown_json = json.dumps(ctx.cost.to_dict())
        run.completed_at = datetime.now(tz=timezone.utc)

        if outcome.verdict == RunVerdict.COMMITTED:
            run.fix_commit_sha = outcome.committed_sha
            run.fix_branch = outcome.committed_branch
            run.fix_strategy = _infer_strategy_from_branch(
                outcome.committed_branch or ""
            )
        elif outcome.verdict == RunVerdict.ESCALATED:
            reason = outcome.escalation_reason.value if outcome.escalation_reason else "unknown"
            run.error = f"{reason}: {outcome.explanation[:500]}"
        elif outcome.verdict == RunVerdict.FAILED:
            run.error = outcome.explanation[:500] or "failed"

        await session.commit()


def _map_verdict_to_status(verdict: RunVerdict) -> str:
    if verdict == RunVerdict.COMMITTED:
        return "COMMITTED"
    if verdict == RunVerdict.ESCALATED:
        return "ESCALATED"
    return "FAILED"


def _infer_strategy_from_branch(branch: str) -> str:
    # Phalanx's fix branches are named phalanx/ci-fix/{run_id}; anything
    # else is the author's branch.
    return "fix_branch" if branch.startswith("phalanx/ci-fix/") else "author_branch"


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


async def execute_v2_run(ci_fix_run_id: str) -> RunOutcome:
    """End-to-end entry point for one v2 run. Called from the Celery
    task dispatcher (wired in Week 1.8+). Returns the RunOutcome so
    callers can feed it into outcome-polling / metrics if needed.
    """
    inputs = await _load_run_inputs(ci_fix_run_id)

    workspace_path = await _clone_workspace(
        ci_fix_run_id=ci_fix_run_id,
        repo_full_name=inputs.repo_full_name,
        branch=inputs.branch,
        github_token=inputs.github_token,
    )
    sandbox_container_id = await _provision_sandbox(workspace_path)

    ctx = AgentContext(
        ci_fix_run_id=ci_fix_run_id,
        repo_full_name=inputs.repo_full_name,
        repo_workspace_path=workspace_path,
        original_failing_command=inputs.original_failing_command,
        pr_number=inputs.pr_number,
        has_write_permission=inputs.has_write_permission,
        ci_api_key=inputs.github_token,
        sandbox_container_id=sandbox_container_id,
        ci_provider=inputs.ci_provider,
        fingerprint_hash=inputs.fingerprint_hash,
        author_head_branch=inputs.branch,
    )

    # Seed the agent's first user message so it has context on turn 1.
    # Without this, the agent sees an empty conversation and burns turns
    # to no effect (root cause of the useless 25-turn loop in run #2).
    ctx.messages.append(_build_initial_user_message(inputs))

    # Wire the Sonnet coder seam — delegate_to_coder → coder_subagent
    # loop → this callable.
    from phalanx.ci_fixer_v2 import coder_subagent as sub_mod

    sub_mod._call_sonnet_llm = _build_sonnet_llm(inputs)

    main_llm = _build_main_llm(inputs)
    outcome = await run_ci_fix_v2(ctx, main_llm)

    finalize_cost_record(ctx.cost)
    await _persist_run_outcome(ci_fix_run_id, ctx, outcome)
    return outcome
