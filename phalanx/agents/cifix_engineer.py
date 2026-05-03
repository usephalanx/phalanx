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
    from phalanx.ci_fixer_v3.task_lifecycle import persist_task_completion  # noqa: PLC0415

    agent = CIFixEngineerAgent(run_id=run_id, agent_id="cifix_engineer", task_id=task_id)
    result = asyncio.run(agent.execute())
    asyncio.run(persist_task_completion(task_id, result))
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixEngineerAgent(BaseAgent):
    AGENT_ROLE = "cifix_engineer"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_engineer.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            ci_context = _parse_ci_context(task.description)
            fix_spec = await self._load_upstream_fix_spec(session)
            integration = await self._load_integration(session, ci_context.get("repo"))
            # Engineer inherits workspace + container_id from the upstream
            # sre_setup task in v3 runs. If absent and we're part of a v3
            # DAG (work_order_type='ci_fix'), we REFUSE the pool fallback
            # — the whole point of v3 is that v2's stale pre-warmed image
            # is the bug we're avoiding. Only the simulate / out-of-band
            # path gets the pool fallback.
            sre_setup = await self._load_sre_setup_output(session)
            is_v3_dag = await self._is_v3_dag_run(session)

        if not fix_spec:
            return AgentResult(
                success=False,
                output={},
                error="upstream cifix_techlead fix_spec not found or invalid",
            )

        # Guard against low-confidence specs FIRST — when TL legitimately says
        # "no code fix possible" (sandbox env mismatch, CI-infra-only failure,
        # PR meta gate, etc.) it sets confidence=0.0 with empty affected_files
        # and may also leave failing_command empty. Checking confidence FIRST
        # gives those cases a clean low_confidence skip rather than a
        # misleading "no failing_command" error. Bug #13 (humanize iter-2,
        # 2026-04-28: TL flagged "uv/uvx missing in sandbox" with conf 0.0 →
        # engineer failed on failing_command guard before the confidence
        # check could run).
        confidence = fix_spec.get("confidence") or 0.0
        if confidence < 0.5:
            return AgentResult(
                success=False,
                output={
                    "committed": False,
                    "skipped_reason": "low_confidence",
                    "tech_lead_confidence": confidence,
                    "tech_lead_open_questions": fix_spec.get("open_questions", []),
                    "tech_lead_root_cause": fix_spec.get("root_cause", ""),
                    "tech_lead_fix_spec": fix_spec.get("fix_spec", ""),
                },
                error=f"Tech Lead confidence {confidence:.2f} below 0.5 threshold",
            )

        # Prefer the exact failing_command Tech Lead observed in the CI log.
        # Fall back to ci_context (simulate path can seed it up-front).
        failing_command = fix_spec.get("failing_command") or ci_context.get("failing_command") or ""
        if not failing_command:
            return AgentResult(
                success=False,
                output={},
                error="no failing_command available from fix_spec or ci_context",
            )
        ci_context["failing_command"] = failing_command

        # v1.5.0 contract — verify_command + verify_success.
        # Backwards-compat: missing fields fall back to v1.4.x behavior
        # (verify_command = failing_command, exit_code == 0 gate).
        # See docs/ci-fixer-v3-agent-contracts.md.
        verify_command = fix_spec.get("verify_command") or failing_command
        verify_success = fix_spec.get("verify_success")  # may be None — gate falls back
        ci_context["verify_command"] = verify_command
        if verify_success is not None:
            ci_context["verify_success"] = verify_success

        affected_files = fix_spec.get("affected_files") or []
        if not affected_files:
            return AgentResult(
                success=False,
                output={},
                error="Tech Lead fix_spec has empty affected_files list",
            )

        # Workspace + sandbox: inherit from sre_setup when available, fall back
        # to self-clone + pool-provision for non-v3 paths (simulate etc.).
        if sre_setup and sre_setup.get("workspace_path") and sre_setup.get("container_id"):
            workspace_path = sre_setup["workspace_path"]
            sandbox_container_id = sre_setup["container_id"]
            self._log.info(
                "cifix_engineer.inherited_sandbox",
                workspace=workspace_path,
                container_id=sandbox_container_id,
            )
        else:
            # Fallback path exists only for simulate + legacy invocation.
            # A v3 DAG run that lost its sre_setup output is a real bug —
            # the whole architectural point of v3 is to avoid the stale
            # pre-warmed pool image. Refusing the fallback here surfaces
            # the bug loudly rather than silently using the wrong sandbox.
            if is_v3_dag:
                return AgentResult(
                    success=False,
                    output={
                        "committed": False,
                        "skipped_reason": "v3_dag_missing_sre_setup",
                    },
                    error=(
                        "v3 DAG is missing upstream cifix_sre setup output; "
                        "refusing to fall back to the pre-warmed pool "
                        "(doing so would defeat the on-the-fly provisioning "
                        "that v3 was built for)"
                    ),
                )
            try:
                workspace_path = await _clone_workspace(
                    run_id=self.run_id,
                    repo_full_name=ci_context["repo"],
                    branch=ci_context["branch"],
                    github_token=_resolve_github_token(integration),
                )
            except Exception as exc:
                self._log.exception("cifix_engineer.clone_failed", error=str(exc))
                return AgentResult(success=False, output={}, error=f"workspace clone failed: {exc}")

            sandbox_container_id = await _provision_sandbox(workspace_path)
            if not sandbox_container_id:
                return AgentResult(
                    success=False,
                    output={},
                    error="sandbox provisioning failed — run_in_sandbox unavailable",
                )
            self._log.info(
                "cifix_engineer.self_provisioned_sandbox_fallback",
                workspace=workspace_path,
                container_id=sandbox_container_id,
                reason="no sre_setup upstream output found (non-v3 path)",
            )

        # ── v1.7 path: deterministic step interpreter ──────────────────
        # When TL emits a task_plan with concrete steps for the engineer,
        # we execute them deterministically instead of running the Sonnet
        # coder loop. This structurally closes Bug #17 (coder turn-cap
        # 0-length diffs). Falls through to the v1.6 Sonnet path when
        # no engineer steps are present.
        v17_engineer_steps = _extract_v17_engineer_steps(fix_spec)
        if v17_engineer_steps:
            return await self._execute_via_step_interpreter(
                steps=v17_engineer_steps,
                workspace_path=workspace_path,
                fix_spec=fix_spec,
                ci_context=ci_context,
                affected_files=affected_files,
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
        # Must pass llm_call explicitly; the default _call_sonnet_llm is a
        # test-only stub that raises NotImplementedError. v2's main-agent
        # bootstrap builds the callable the same way — see
        # ci_fixer_v2.run_bootstrap._build_sonnet_llm.
        from phalanx.ci_fixer_v2.coder_subagent import (  # noqa: PLC0415
            run_coder_subagent,
        )
        from phalanx.ci_fixer_v2.prompts import (  # noqa: PLC0415
            CODER_SUBAGENT_SYSTEM_PROMPT,
        )
        from phalanx.ci_fixer_v2.providers import (  # noqa: PLC0415
            build_sonnet_coder_callable,
        )
        from phalanx.ci_fixer_v2.tool_scopes import (  # noqa: PLC0415
            coder_subagent_tool_schemas,
        )

        settings = get_settings()
        sonnet_llm = build_sonnet_coder_callable(
            model=settings.anthropic_model_ci_fixer_coder,
            api_key=settings.anthropic_api_key,
            system_prompt=CODER_SUBAGENT_SYSTEM_PROMPT,
            tool_schemas=coder_subagent_tool_schemas(),
        )

        # v1.5.0: pass verify_command (what should run to confirm) as the
        # failing_command param. Coder uses this for the post-patch sandbox
        # gate. Backwards compat: when fix_spec lacks verify_command, this
        # equals failing_command (today's behavior).
        coder_result = await run_coder_subagent(
            ctx=ctx,
            task_description=fix_spec["fix_spec"],
            target_files=affected_files,
            diagnosis_summary=fix_spec.get("root_cause", ""),
            failing_command=ci_context.get("verify_command") or ci_context["failing_command"],
            llm_call=sonnet_llm,
        )

        tokens_used = coder_result.sonnet_input_tokens + coder_result.sonnet_output_tokens

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
            self._log.error("cifix_engineer.commit_failed", error=commit_result.error)
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

        # Bug #11 mitigation A1 (bot-loop guard): record the fix commit on the
        # CIFixRun so the webhook handler can recognize CI runs triggered by
        # our own push and skip dispatching a parallel v3 run for them. The
        # existing sender.login filter is dead code on token-based pushes —
        # webhook sender is the PAT owner, not the git author. Matching
        # head_sha → CIFixRun.fix_commit_sha is the reliable signal.
        ci_fix_run_id = ci_context.get("ci_fix_run_id")
        if ci_fix_run_id and commit_sha:
            try:
                from sqlalchemy import update  # noqa: PLC0415

                from phalanx.db.models import CIFixRun  # noqa: PLC0415

                async with get_db() as session:
                    await session.execute(
                        update(CIFixRun)
                        .where(CIFixRun.id == ci_fix_run_id)
                        .values(fix_commit_sha=commit_sha)
                    )
                    await session.commit()
            except Exception as exc:
                # Best-effort. Don't fail the engineer task on this — the
                # fix already shipped to the customer's PR. The webhook
                # bot-loop guard fails open: a missing fix_commit_sha just
                # means we MIGHT dispatch a wasteful second run.
                self._log.warning(
                    "cifix_engineer.fix_commit_sha_update_failed",
                    ci_fix_run_id=ci_fix_run_id,
                    error=str(exc),
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

    # ── v1.7 step-interpreter path ───────────────────────────────────────────

    async def _execute_via_step_interpreter(
        self,
        *,
        steps: list[dict],
        workspace_path: str,
        fix_spec: dict,
        ci_context: dict,
        affected_files: list[str],
    ) -> AgentResult:
        """v1.7 deterministic execution of TL-emitted engineer steps.

        TL planned every step explicitly (replace/insert/apply_diff/run/
        commit/push). We walk them via execute_task_steps. If any step's
        precondition fails (most commonly a stale `replace.old`), we
        report `step_precondition_violated` and TL's re-plan loop kicks
        in upstream.

        On success: extract commit_sha from the last commit step, update
        CIFixRun.fix_commit_sha (same bot-loop guard as v1.6 path),
        return AgentResult mirroring the v1.6 success shape so commander
        and downstream readers don't need to special-case.
        """
        from phalanx.agents._engineer_step_interpreter import (  # noqa: PLC0415
            execute_task_steps_async,
        )

        self._log.info(
            "cifix_engineer.v17_path",
            n_steps=len(steps),
            workspace=workspace_path,
            allowed_files=affected_files,
        )
        # v1.7.2.3: pass affected_files as the patch-safety allowlist so
        # the engineer can't drift outside what TL declared.
        result = await execute_task_steps_async(
            steps, workspace_path, allowed_files=affected_files,
        )

        if not result.ok:
            failed = result.failed_step
            assert failed is not None
            self._log.warning(
                "cifix_engineer.v17_step_failed",
                step_id=failed.step_id,
                action=failed.action,
                error=failed.error,
                detail=(failed.detail or "")[:300],
            )
            return AgentResult(
                success=False,
                output={
                    "committed": False,
                    "v17_path": True,
                    "failed_step_id": failed.step_id,
                    "failed_step_action": failed.action,
                    "failed_step_error": failed.error,
                    "failed_step_detail": failed.detail,
                    "completed_steps": result.completed_steps,
                    "tech_lead_root_cause": fix_spec.get("root_cause", ""),
                    "tech_lead_fix_spec": fix_spec.get("fix_spec", ""),
                },
                error=(
                    f"step {failed.step_id} ({failed.action}) failed: "
                    f"{failed.error}"
                ),
            )

        commit_sha = result.commit_sha
        if commit_sha is None:
            # Steps completed but no commit step ran — TL's plan was
            # incomplete (missing commit / push). Surface as failure so
            # commander can re-route to TL.
            self._log.warning(
                "cifix_engineer.v17_no_commit_in_plan",
                completed_steps=result.completed_steps,
            )
            return AgentResult(
                success=False,
                output={
                    "committed": False,
                    "v17_path": True,
                    "skipped_reason": "tl_plan_missing_commit_step",
                    "completed_steps": result.completed_steps,
                },
                error=(
                    "TL's engineer task_plan completed all steps but did "
                    "not include a `commit` action — refusing to claim "
                    "success without a commit_sha"
                ),
            )

        # Bot-loop guard parity with v1.6 path: record fix_commit_sha
        # on CIFixRun so the webhook handler skips dispatching parallel
        # v3 runs for our own push.
        await self._update_fix_commit_sha(ci_context, commit_sha)

        # Compute unified diff for scorecard / observability (parity).
        unified_diff = ""
        try:
            from phalanx.ci_fixer_v2.tools.coder import _compute_final_diff  # noqa: PLC0415

            unified_diff = await _compute_final_diff(workspace_path)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("cifix_engineer.v17_diff_compute_failed", error=str(exc))

        self._log.info(
            "cifix_engineer.v17_committed",
            commit_sha=commit_sha,
            n_steps=len(result.completed_steps),
        )
        return AgentResult(
            success=True,
            output={
                "committed": True,
                "v17_path": True,
                "commit_sha": commit_sha,
                "files_modified": affected_files,
                "diff": unified_diff,
                "completed_steps": result.completed_steps,
                "verify": {
                    "cmd": ci_context.get("verify_command")
                    or ci_context.get("failing_command"),
                    "exit_code": 0,
                },
                "model": "deterministic-step-interpreter",
                "tokens_used": 0,
            },
            tokens_used=0,
        )

    async def _update_fix_commit_sha(self, ci_context: dict, commit_sha: str) -> None:
        """Mirror of the v1.6 fix_commit_sha update — best-effort."""
        ci_fix_run_id = ci_context.get("ci_fix_run_id")
        if not (ci_fix_run_id and commit_sha):
            return
        try:
            from sqlalchemy import update  # noqa: PLC0415

            from phalanx.db.models import CIFixRun  # noqa: PLC0415

            async with get_db() as session:
                await session.execute(
                    update(CIFixRun)
                    .where(CIFixRun.id == ci_fix_run_id)
                    .values(fix_commit_sha=commit_sha)
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "cifix_engineer.v17_fix_commit_sha_update_failed",
                ci_fix_run_id=ci_fix_run_id,
                error=str(exc),
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
        if not all(k in output for k in ("root_cause", "affected_files", "fix_spec", "confidence")):
            return None
        return output

    async def _load_integration(self, session, repo: str | None) -> CIIntegration | None:
        if not repo:
            return None
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.repo_full_name == repo)
        )
        return result.scalar_one_or_none()

    async def _is_v3_dag_run(self, session) -> bool:
        """True iff this task belongs to a WorkOrder with work_order_type='ci_fix'.

        Used to decide whether the pool fallback is acceptable (simulate /
        out-of-band path → yes) or a sign of a real bug (v3 DAG → no).
        """
        from phalanx.db.models import Run, WorkOrder  # noqa: PLC0415

        result = await session.execute(
            select(WorkOrder.work_order_type)
            .select_from(WorkOrder)
            .join(Run, Run.work_order_id == WorkOrder.id)
            .where(Run.id == self.run_id)
        )
        row = result.one_or_none()
        return bool(row and row[0] == "ci_fix")

    async def _load_sre_setup_output(self, session) -> dict | None:
        """Find the earliest COMPLETED cifix_sre task with mode='setup' in this run.

        Iteration 2+ reads the SAME setup task as iteration 1 (we reuse the
        container_id across iterations — see commander._append_iteration_dag).
        """
        result = await session.execute(
            select(Task.output)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role.in_(
                    ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                ),
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.asc())
        )
        for (output,) in result.all():
            if isinstance(output, dict) and output.get("mode") == "setup":
                return output
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Module helpers (reuse v2 clone + sandbox + context; kept as module-level
# functions so unit tests can inject fakes)
# ─────────────────────────────────────────────────────────────────────────────


def _extract_v17_engineer_steps(fix_spec: dict) -> list[dict] | None:
    """v1.7 dispatch helper — returns the engineer task's `steps` list
    when TL emitted a well-formed task_plan, else None.

    Falls back gracefully: if fix_spec is v1.6-shape (no task_plan),
    returns None and the engineer takes the Sonnet coder_subagent path.
    Same if task_plan exists but contains no engineer task, or the
    engineer task has no steps. This keeps the v1.6 testbed canaries
    working unchanged through the cutover.
    """
    plan = fix_spec.get("task_plan")
    if not isinstance(plan, list):
        return None
    for ts in plan:
        if not isinstance(ts, dict):
            continue
        if ts.get("agent") != "cifix_engineer":
            continue
        steps = ts.get("steps")
        if isinstance(steps, list) and steps:
            return steps
    return None


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

    # v1.5.0: original_failing_command becomes the verify_command (which is
    # what the engineer ACTUALLY runs to confirm the fix). For backwards
    # compat, ci_context.verify_command was populated upstream from
    # fix_spec.verify_command OR fix_spec.failing_command.
    return AgentContext(
        ci_fix_run_id=f"v3-{run_id}",
        repo_full_name=ci_context["repo"],
        repo_workspace_path=workspace_path,
        original_failing_command=ci_context.get("verify_command") or ci_context["failing_command"],
        pr_number=ci_context.get("pr_number"),
        has_write_permission=True,  # Engineer can commit to the PR branch
        ci_api_key=_resolve_github_token(integration),
        ci_provider=(integration.ci_provider if integration else "github_actions"),
        author_head_branch=ci_context.get("branch"),
        sandbox_container_id=sandbox_container_id,
        verify_success_criteria=ci_context.get("verify_success"),
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
    return f"fix(ci): {subject}\n\n{body_excerpt}\n\nCI Fixer v3 • failing job: {failing_job}"
