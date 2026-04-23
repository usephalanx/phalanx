"""CI Fixer v3 — Engineer agent (implementer).

Phase 1 implementation. Sonnet code edits + sandbox verify + deterministic commit.

Role:
  - Reads the upstream cifix_techlead Task.output.fix_spec from the same run.
  - Shallow-clones the repo at the PR head branch; provisions a sandbox.
  - Invokes v2's run_coder_subagent(Sonnet) with {fix_spec, affected_files,
    failing_command} to apply the edit and verify in sandbox.
  - On verified exit 0: computes the unified diff, dispatches commit_and_push
    DETERMINISTICALLY (not via LLM — the commit decision is not negotiable
    once verification passes).
  - Writes structured output to tasks.output so cifix_commander can read it.

Invariants:
  - Never investigates (no fetch_ci_log / get_pr_diff / git_blame).
  - Never second-guesses the fix_spec. Whatever Tech Lead said is gospel.
  - Never commits without a green sandbox run of the exact failing command.
  - Single Celery task invocation; no outer retry. If verification fails,
    the run FAILS and cifix_commander decides whether to re-dispatch
    (Phase 2 iteration loop).

Output (Task.output):
  {
    "committed": bool,
    "commit_sha": str | None,
    "files_modified": [str],
    "diff": str,                      # unified diff (for audit + scorecard)
    "verify": {"cmd": str, "exit_code": int},
    "coder_attempts": int,
    "model": "sonnet-4-6",
    "tokens_used": int,
  }
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog
from sqlalchemy import select

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.config.settings import get_settings
from phalanx.db.models import CIIntegration, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(
    name="phalanx.agents.cifix_engineer.execute_task",
    bind=True,
    queue="cifix_engineer",
    max_retries=1,
    soft_time_limit=900,
    time_limit=1020,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:  # pragma: no cover
    agent = CIFixEngineerAgent(run_id=run_id, agent_id="cifix_engineer", task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixEngineerAgent(BaseAgent):
    AGENT_ROLE = "cifix_engineer"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_engineer.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(
                    success=False, output={}, error=f"Task {self.task_id} not found"
                )
            ci_context = _parse_ci_context(task.description)
            fix_spec = await self._load_upstream_fix_spec(session)
            integration = await self._load_integration(
                session, ci_context.get("repo")
            )

        if not fix_spec:
            return AgentResult(
                success=False,
                output={},
                error="upstream cifix_techlead fix_spec not found or invalid",
            )
        if not ci_context.get("failing_command"):
            return AgentResult(
                success=False, output={}, error="ci_context missing failing_command"
            )

        # Guard against low-confidence specs — escalate without attempting.
        # Tech Lead is supposed to flag confidence < 0.5 as open_questions; we
        # harden that here. Phase 2 commander can re-dispatch Tech Lead.
        confidence = fix_spec.get("confidence") or 0.0
        if confidence < 0.35:
            return AgentResult(
                success=False,
                output={
                    "committed": False,
                    "skipped_reason": "low_confidence",
                    "tech_lead_confidence": confidence,
                    "tech_lead_open_questions": fix_spec.get("open_questions", []),
                },
                error=f"Tech Lead confidence {confidence:.2f} below 0.35 threshold",
            )

        affected_files = fix_spec.get("affected_files") or []
        if not affected_files:
            return AgentResult(
                success=False,
                output={},
                error="Tech Lead fix_spec has empty affected_files list",
            )

        # Clone + sandbox
        try:
            workspace_path = await _clone_workspace(
                run_id=self.run_id,
                repo_full_name=ci_context["repo"],
                branch=ci_context["branch"],
                github_token=_resolve_github_token(integration),
            )
        except Exception as exc:
            self._log.exception("cifix_engineer.clone_failed", error=str(exc))
            return AgentResult(
                success=False, output={}, error=f"workspace clone failed: {exc}"
            )

        sandbox_container_id = await _provision_sandbox(workspace_path)
        if not sandbox_container_id:
            return AgentResult(
                success=False,
                output={},
                error="sandbox provisioning failed — run_in_sandbox unavailable",
            )

        # Build AgentContext with sandbox available
        ctx = _build_engineer_context(
            run_id=self.run_id,
            ci_context=ci_context,
            workspace_path=workspace_path,
            sandbox_container_id=sandbox_container_id,
            integration=integration,
        )

        # Invoke v2 coder subagent — this is the Sonnet edit+verify loop.
        from phalanx.ci_fixer_v2.coder_subagent import run_coder_subagent  # noqa: PLC0415

        coder_result = await run_coder_subagent(
            ctx=ctx,
            task_description=fix_spec["fix_spec"],
            target_files=affected_files,
            diagnosis_summary=fix_spec.get("root_cause", ""),
            failing_command=ci_context["failing_command"],
        )

        tokens_used = (
            coder_result.sonnet_input_tokens + coder_result.sonnet_output_tokens
        )

        if not coder_result.success or not ctx.last_sandbox_verified:
            # Coder tried but couldn't produce a verified diff — do NOT commit.
            self._log.warning(
                "cifix_engineer.verify_failed",
                sandbox_exit=coder_result.sandbox_exit_code,
                attempts=coder_result.attempts_used,
            )
            return AgentResult(
                success=False,
                output={
                    "committed": False,
                    "verify": {
                        "cmd": ci_context["failing_command"],
                        "exit_code": coder_result.sandbox_exit_code,
                    },
                    "coder_attempts": coder_result.attempts_used,
                    "sandbox_stderr_tail": coder_result.sandbox_stderr_tail,
                    "sandbox_stdout_tail": coder_result.sandbox_stdout_tail,
                    "notes": coder_result.notes,
                },
                error="coder could not verify the fix in sandbox",
                tokens_used=tokens_used,
            )

        # Verified. Compute diff BEFORE commit (for the Task output + scorecard).
        from phalanx.ci_fixer_v2.tools.coder import _compute_final_diff  # noqa: PLC0415

        unified_diff = await _compute_final_diff(ctx.repo_workspace_path)

        # Deterministic commit_and_push. NOT an LLM decision.
        from phalanx.ci_fixer_v2.tools.action import (  # noqa: PLC0415
            _handle_commit_and_push,
        )

        commit_message = _build_commit_message(fix_spec, ci_context)
        commit_result = await _handle_commit_and_push(
            ctx,
            {
                "branch_strategy": "author_branch",
                "commit_message": commit_message,
                "files": affected_files,
            },
        )
        if not commit_result.ok:
            self._log.error(
                "cifix_engineer.commit_failed", error=commit_result.error
            )
            return AgentResult(
                success=False,
                output={
                    "committed": False,
                    "verify": {
                        "cmd": ci_context["failing_command"],
                        "exit_code": 0,
                    },
                    "diff": unified_diff,
                    "commit_error": commit_result.error,
                },
                error=f"commit_and_push failed after verified fix: {commit_result.error}",
                tokens_used=tokens_used,
            )

        commit_sha = commit_result.data.get("sha")
        self._log.info(
            "cifix_engineer.committed",
            sha=commit_sha,
            files=affected_files,
            attempts=coder_result.attempts_used,
        )

        return AgentResult(
            success=True,
            output={
                "committed": True,
                "commit_sha": commit_sha,
                "files_modified": affected_files,
                "diff": unified_diff,
                "verify": {"cmd": ci_context["failing_command"], "exit_code": 0},
                "coder_attempts": coder_result.attempts_used,
                "model": "sonnet-4-6",
                "tokens_used": tokens_used,
            },
            tokens_used=tokens_used,
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_upstream_fix_spec(self, session) -> dict | None:
        """Find the latest COMPLETED cifix_techlead Task in this run + return its output."""
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "cifix_techlead",
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.desc())
            .limit(1)
        )
        row = result.one_or_none()
        if row is None or row[0] is None:
            return None
        output = row[0]
        # Minimum-viable validation — Tech Lead wrote this, but be paranoid.
        if not isinstance(output, dict):
            return None
        if not all(
            k in output
            for k in ("root_cause", "affected_files", "fix_spec", "confidence")
        ):
            return None
        return output

    async def _load_integration(self, session, repo: str | None) -> CIIntegration | None:
        if not repo:
            return None
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.repo_full_name == repo)
        )
        return result.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# Module helpers (reuse v2 clone + sandbox + context; kept as module-level
# functions so unit tests can inject fakes)
# ─────────────────────────────────────────────────────────────────────────────


async def _clone_workspace(
    run_id: str, repo_full_name: str, branch: str, github_token: str | None
) -> str:
    """Shallow clone at the PR head branch. Engineer's own workspace — the
    Tech Lead cloned its own earlier in a different Celery task."""
    if not github_token:
        raise RuntimeError("no github token available for clone")
    import git  # noqa: PLC0415

    base = Path(get_settings().git_workspace) / f"v3-{run_id}-engineer"
    if base.exists():
        import shutil  # noqa: PLC0415

        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    git.Repo.clone_from(url, base, branch=branch, depth=1)
    return str(base)


async def _provision_sandbox(workspace_path: str) -> str | None:
    """Spin up a Docker sandbox against the cloned workspace. Returns the
    container id, or None if unavailable (no fallback — refuse to commit)."""
    from phalanx.ci_fixer.sandbox import SandboxProvisioner  # noqa: PLC0415

    provisioner = SandboxProvisioner()
    sandbox = await provisioner.provision(Path(workspace_path))
    if sandbox is None or not sandbox.available:
        return None
    return sandbox.container_id or None


def _build_engineer_context(
    run_id: str,
    ci_context: dict,
    workspace_path: str,
    sandbox_container_id: str,
    integration: CIIntegration | None,
):
    from phalanx.ci_fixer_v2.context import AgentContext  # noqa: PLC0415

    return AgentContext(
        ci_fix_run_id=f"v3-{run_id}",
        repo_full_name=ci_context["repo"],
        repo_workspace_path=workspace_path,
        original_failing_command=ci_context["failing_command"],
        pr_number=ci_context.get("pr_number"),
        has_write_permission=True,  # Engineer can commit to the PR branch
        ci_api_key=_resolve_github_token(integration),
        ci_provider=(integration.ci_provider if integration else "github_actions"),
        author_head_branch=ci_context.get("branch"),
        sandbox_container_id=sandbox_container_id,
    )


def _resolve_github_token(integration: CIIntegration | None) -> str | None:
    if integration and integration.github_token:
        return integration.github_token
    return get_settings().github_token or None


def _parse_ci_context(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _build_commit_message(fix_spec: dict, ci_context: dict) -> str:
    """Concise commit message for the PR branch.

    Shape:
      fix(ci): <root cause, 72-char max>

      <fix_spec excerpt>

      CI Fixer v3 • run=<run_id>
    """
    root_cause = (fix_spec.get("root_cause") or "CI failure").strip()
    subject = root_cause[:72].rstrip(".")
    body_excerpt = (fix_spec.get("fix_spec") or "").strip()
    if len(body_excerpt) > 400:
        body_excerpt = body_excerpt[:400].rstrip() + "…"
    failing_job = ci_context.get("failing_job_name") or "?"
    return (
        f"fix(ci): {subject}\n\n"
        f"{body_excerpt}\n\n"
        f"CI Fixer v3 • failing job: {failing_job}"
    )
