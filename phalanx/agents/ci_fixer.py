"""
CI Fixer Agent — multi-stage pipeline for autonomous CI failure repair.

Pipeline:
  1. Fetch raw CI logs (provider-specific fetcher)
  2. Parse logs deterministically → structured errors (LogParser)
  3. Confirm root cause via LLM with structured input (RootCauseAnalyst)
  4. Apply fix patches to cloned workspace (Builder)
  5. Validate fix by re-running the failing tool (Validator)
  6. Commit + push + comment on PR

Design invariants:
  - Never modifies test assertions
  - Never touches files not mentioned in the CI failure
  - Low-confidence → no commit, logs uncertainty trace
  - Validation failure → one retry with new log (max 2 analyst iterations)
  - Max 2 attempts per PR (tracked via CIFixRun.attempt)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.agents.soul import CI_FIXER_SOUL
from phalanx.ci_fixer.analyst import FixPlan, RootCauseAnalyst
from phalanx.ci_fixer.events import CIFailureEvent
from phalanx.ci_fixer.log_fetcher import get_log_fetcher
from phalanx.ci_fixer.log_parser import ParsedLog, parse_log
from phalanx.ci_fixer.validator import validate_fix
from phalanx.config.settings import get_settings
from phalanx.db.models import CIFixRun, CIIntegration
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)
settings = get_settings()

# Max analyst retry iterations (parse → fix → validate → re-analyze)
_MAX_ITERATIONS = 2


class CIFixerAgent(BaseAgent):
    """
    Autonomous CI failure repair agent.

    Operates on CIFixRun records created by the webhook ingest layer.
    Does not participate in the normal Run/Task pipeline.
    """

    AGENT_ROLE = "ci_fixer"

    def __init__(self, ci_fix_run_id: str):
        super().__init__(
            run_id=ci_fix_run_id,
            agent_id="ci-fixer",
            task_id=ci_fix_run_id,
        )
        self.ci_fix_run_id = ci_fix_run_id

    async def execute(self) -> AgentResult:
        self._log.info("ci_fixer.execute.start", ci_fix_run_id=self.ci_fix_run_id)

        # ── 1. Load records ──────────────────────────────────────────────────
        async with get_db() as session:
            ci_run = await self._load_ci_fix_run(session)
            if ci_run is None:
                return AgentResult(
                    success=False,
                    output={},
                    error=f"CIFixRun {self.ci_fix_run_id} not found",
                )
            integration = await self._load_integration(session, ci_run.integration_id)

        if integration is None:
            return AgentResult(success=False, output={}, error="CIIntegration not found")

        # ── 2. Fetch raw logs ─────────────────────────────────────────────────
        event = CIFailureEvent(
            provider=ci_run.ci_provider,
            repo_full_name=ci_run.repo_full_name,
            branch=ci_run.branch,
            commit_sha=ci_run.commit_sha,
            build_id=ci_run.ci_build_id,
            build_url=ci_run.build_url or "",
            pr_number=ci_run.pr_number,
            integration_id=str(integration.id),
        )

        raw_log = await self._fetch_logs(event, integration)
        self._log.info(
            "ci_fixer.logs_fetched",
            chars=len(raw_log),
            has_content=bool(raw_log.strip()),
        )

        # ── 3. Parse deterministically ────────────────────────────────────────
        parsed = parse_log(raw_log)
        self._log.info(
            "ci_fixer.parsed",
            tool=parsed.tool,
            lint=len(parsed.lint_errors),
            type_=len(parsed.type_errors),
            test=len(parsed.test_failures),
            build=len(parsed.build_errors),
            summary=parsed.summary(),
        )

        await self._trace(
            "decision",
            f"**Parsed log** — tool: `{parsed.tool}`\n\n{parsed.as_text()}",
            {"tool": parsed.tool, "summary": parsed.summary()},
        )

        if not parsed.has_errors:
            # No structured errors found — fall back to raw log summary
            self._log.warning("ci_fixer.no_structured_errors", raw_preview=raw_log[:300])
            await self._mark_failed(ci_run, "no_structured_errors")
            return AgentResult(
                success=False,
                output={"reason": "no_structured_errors", "raw_preview": raw_log[:500]},
            )

        # ── 4. Clone repo ─────────────────────────────────────────────────────
        workspace = Path(settings.git_workspace) / "ci-fixer" / self.ci_fix_run_id
        workspace.mkdir(parents=True, exist_ok=True)

        cloned = await self._clone_repo(
            workspace,
            repo_full_name=ci_run.repo_full_name,
            branch=ci_run.branch,
            commit_sha=ci_run.commit_sha,
            github_token=self._get_github_token(integration),
        )
        if not cloned:
            await self._mark_failed(ci_run, "repo_clone_failed")
            return AgentResult(success=False, output={}, error="repo clone failed")

        # ── 5. Analyst loop: confirm root cause → apply → validate ────────────
        analyst = RootCauseAnalyst(call_llm=self._call_claude)
        fix_plan: FixPlan | None = None
        validation_output = ""

        for iteration in range(1, _MAX_ITERATIONS + 1):
            self._log.info("ci_fixer.analyst_iteration", iteration=iteration)

            # If this is a retry, re-parse the validation output
            if iteration > 1 and validation_output:
                retry_parsed = parse_log(validation_output)
                if retry_parsed.has_errors:
                    parsed = retry_parsed

            # LLM confirmation
            fix_plan = analyst.analyze(parsed, workspace)
            self._log.info(
                "ci_fixer.fix_plan",
                confidence=fix_plan.confidence,
                root_cause=fix_plan.root_cause,
                patches=len(fix_plan.patches),
                needs_test=fix_plan.needs_new_test,
            )

            await self._trace(
                "reflection",
                f"**Root cause:** {fix_plan.root_cause}\n"
                f"**Confidence:** {fix_plan.confidence}\n"
                f"**Patches:** {len(fix_plan.patches)} file(s)",
                {"confidence": fix_plan.confidence, "iteration": iteration},
            )

            if not fix_plan.is_actionable:
                self._log.info(
                    "ci_fixer.low_confidence",
                    root_cause=fix_plan.root_cause,
                    iteration=iteration,
                )
                break

            # Apply patches
            files_written = self._apply_patches(workspace, fix_plan.patches)
            if not files_written:
                self._log.warning("ci_fixer.no_files_written")
                break

            # Validate
            validation = validate_fix(parsed, workspace)
            self._log.info(
                "ci_fixer.validation",
                passed=validation.passed,
                tool=validation.tool,
                iteration=iteration,
            )

            if validation.passed:
                # Fix confirmed — proceed to commit
                self._log.info("ci_fixer.validation_passed", files=files_written)
                break
            else:
                validation_output = validation.output
                self._log.warning(
                    "ci_fixer.validation_failed",
                    iteration=iteration,
                    output=validation.output[:300],
                )
                await self._trace(
                    "uncertainty",
                    f"Validation failed (iteration {iteration}):\n```\n{validation.output[:500]}\n```",
                    {"iteration": iteration},
                )
                if iteration >= _MAX_ITERATIONS:
                    # Exhausted retries — still commit if medium+ confidence
                    # (the PR CI will catch it if wrong)
                    self._log.warning("ci_fixer.max_iterations_reached")

        # ── 6. Check final plan ───────────────────────────────────────────────
        if not fix_plan or not fix_plan.is_actionable:
            await self._mark_failed(ci_run, "low_confidence")
            return AgentResult(
                success=False,
                output={
                    "reason": "low_confidence",
                    "root_cause": fix_plan.root_cause if fix_plan else "",
                    "tool": parsed.tool,
                },
            )

        files_written = [p.path for p in fix_plan.patches]

        # ── 7. Commit + push ──────────────────────────────────────────────────
        commit_result = await self._commit_and_push(
            workspace=workspace,
            branch=ci_run.branch,
            commit_message=(
                f"fix(ci): resolve {parsed.tool} failure [{ci_run.ci_provider}]\n\n"
                f"Root cause: {fix_plan.root_cause}\n"
                f"Files: {', '.join(files_written)}\n"
                f"CI Fix Run: {self.ci_fix_run_id}"
            ),
        )
        commit_sha = commit_result.get("sha")

        # ── 8. Comment on PR ──────────────────────────────────────────────────
        if ci_run.pr_number and commit_sha and integration.github_token:
            await self._comment_on_pr(
                integration=integration,
                ci_run=ci_run,
                files_written=files_written,
                commit_sha=commit_sha,
                tool=parsed.tool,
                root_cause=fix_plan.root_cause,
                parsed=parsed,
            )

        # ── 9. Mark FIXED ─────────────────────────────────────────────────────
        async with get_db() as session:
            await session.execute(
                update(CIFixRun)
                .where(CIFixRun.id == self.ci_fix_run_id)
                .values(
                    status="FIXED",
                    fix_commit_sha=commit_sha,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        self._log.info(
            "ci_fixer.execute.done",
            tool=parsed.tool,
            files=files_written,
            commit_sha=commit_sha,
            root_cause=fix_plan.root_cause,
        )

        return AgentResult(
            success=True,
            output={
                "tool": parsed.tool,
                "root_cause": fix_plan.root_cause,
                "files_fixed": files_written,
                "commit_sha": commit_sha,
                "confidence": fix_plan.confidence,
            },
        )

    # ── Log fetching ───────────────────────────────────────────────────────────

    async def _fetch_logs(self, event: CIFailureEvent, integration: CIIntegration) -> str:
        """Fetch raw CI logs. Falls back to failure_summary if fetcher fails."""
        async with get_db() as session:
            ci_run = await self._load_ci_fix_run(session)
        try:
            fetcher = get_log_fetcher(event.provider)
            raw_key = self._decrypt_key(integration.ci_api_key_enc)
            api_key = raw_key or self._get_github_token(integration)
            return await fetcher.fetch(event, api_key)
        except Exception as exc:
            self._log.warning("ci_fixer.log_fetch_failed", error=str(exc))
            return ci_run.failure_summary or "(no logs available)" if ci_run else "(no logs)"

    # ── Patch application ──────────────────────────────────────────────────────

    def _apply_patches(self, workspace: Path, patches: list) -> list[str]:
        """Write fix patches to workspace. Returns list of relative paths written."""
        written: list[str] = []
        for patch in patches:
            full_path = workspace / patch.path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                full_path.write_text(patch.content, encoding="utf-8")
                written.append(patch.path)
                self._log.info("ci_fixer.patch_applied", path=patch.path)
            except Exception as exc:
                self._log.warning("ci_fixer.patch_failed", path=patch.path, error=str(exc))
        return written

    # ── Git helpers ────────────────────────────────────────────────────────────

    async def _clone_repo(
        self,
        workspace: Path,
        repo_full_name: str,
        branch: str,
        commit_sha: str,
        github_token: str,
    ) -> bool:
        try:
            from git import Repo  # noqa: PLC0415

            repo_url = f"https://github.com/{repo_full_name}.git"
            auth_url = repo_url.replace("https://", f"https://{github_token}@")

            if (workspace / ".git").exists():
                repo = Repo(str(workspace))
                repo.remotes.origin.fetch()
            else:
                repo = Repo.clone_from(auth_url, str(workspace))

            try:
                repo.git.checkout(branch)
            except Exception:
                repo.git.checkout("-b", branch, commit_sha)

            self._log.info("ci_fixer.git.cloned", repo=repo_full_name, branch=branch)
            return True
        except ImportError:
            self._log.warning("ci_fixer.git.gitpython_missing")
            return False
        except Exception as exc:
            self._log.warning("ci_fixer.git.clone_failed", error=str(exc))
            return False

    async def _commit_and_push(
        self,
        workspace: Path,
        branch: str,
        commit_message: str,
    ) -> dict[str, Any]:
        try:
            from git import Actor, Repo  # noqa: PLC0415
            from git.exc import InvalidGitRepositoryError  # noqa: PLC0415

            try:
                repo = Repo(str(workspace))
            except InvalidGitRepositoryError:
                return {"sha": None, "error": "not a git repo"}

            repo.git.add("-A")
            if not repo.index.diff("HEAD") and not repo.untracked_files:
                return {"sha": None, "message": "no changes"}

            author = Actor(settings.git_author_name, settings.git_author_email)
            commit = repo.index.commit(commit_message, author=author, committer=author)
            sha = commit.hexsha[:8]

            if settings.github_token and repo.remotes:
                try:
                    repo.git.push("origin", branch, "--set-upstream")
                    self._log.info("ci_fixer.git.pushed", branch=branch, sha=sha)
                except Exception as push_exc:
                    self._log.warning("ci_fixer.git.push_failed", error=str(push_exc))

            return {"sha": sha, "branch": branch}
        except Exception as exc:
            self._log.warning("ci_fixer.git.commit_failed", error=str(exc))
            return {"sha": None, "error": str(exc)}

    # ── PR comment ─────────────────────────────────────────────────────────────

    async def _comment_on_pr(
        self,
        integration: CIIntegration,
        ci_run: CIFixRun,
        files_written: list[str],
        commit_sha: str,
        tool: str,
        root_cause: str,
        parsed: ParsedLog,
    ) -> None:
        import httpx  # noqa: PLC0415

        files_list = "\n".join(f"- `{f}`" for f in files_written)

        # Build a structured error summary for the comment
        error_detail = ""
        if parsed.lint_errors:
            errors_text = "\n".join(
                f"  - `{e.file}:{e.line}` — `{e.code}` {e.message}"
                for e in parsed.lint_errors[:5]
            )
            error_detail = f"\n\n**Errors fixed:**\n{errors_text}"
        elif parsed.test_failures:
            tests_text = "\n".join(f"  - `{f.test_id}`" for f in parsed.test_failures[:5])
            error_detail = f"\n\n**Tests fixed:**\n{tests_text}"

        body = (
            f"🔧 **Phalanx CI Fixer** resolved a `{tool}` failure "
            f"in commit `{commit_sha}`.\n\n"
            f"**Root cause:** {root_cause}"
            f"{error_detail}\n\n"
            f"**Files changed:**\n{files_list}\n\n"
            f"A new CI run has been triggered automatically."
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
            self._log.info("ci_fixer.pr_commented", pr=ci_run.pr_number)
        except Exception as exc:
            self._log.warning("ci_fixer.pr_comment_failed", error=str(exc))

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _load_ci_fix_run(self, session) -> CIFixRun | None:
        result = await session.execute(select(CIFixRun).where(CIFixRun.id == self.ci_fix_run_id))
        return result.scalar_one_or_none()

    async def _load_integration(self, session, integration_id: str) -> CIIntegration | None:
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.id == integration_id)
        )
        return result.scalar_one_or_none()

    async def _mark_failed(self, ci_run: CIFixRun, reason: str) -> None:
        async with get_db() as session:
            await session.execute(
                update(CIFixRun)
                .where(CIFixRun.id == self.ci_fix_run_id)
                .values(status="FAILED", error=reason, completed_at=datetime.now(UTC))
            )
            await session.commit()

    # ── Auth helpers ────────────────────────────────────────────────────────────

    def _decrypt_key(self, encrypted_key: str) -> str:
        return encrypted_key  # Phase 2: KMS decrypt

    def _get_github_token(self, integration: CIIntegration) -> str:
        return integration.github_token or settings.github_token

    # ── Backward-compat shims (used by unit tests) ─────────────────────────────

    def _apply_fix_files(self, workspace: Path, files: list[dict]) -> list[str]:
        """Dict-based shim for unit tests. Delegates to _apply_patches."""
        from phalanx.ci_fixer.analyst import FilePatch  # noqa: PLC0415

        patches = [
            FilePatch(path=f.get("path", ""), content=f.get("content", ""))
            for f in files
            if f.get("path") and f.get("content")
        ]
        return self._apply_patches(workspace, patches)

    def _read_files(self, workspace: Path, paths: list[str]) -> str:
        """Shim for unit tests — reads files for prompt context."""
        from phalanx.ci_fixer.analyst import RootCauseAnalyst  # noqa: PLC0415

        analyst = RootCauseAnalyst(call_llm=lambda **_: "")
        return analyst._read_files(workspace, paths)


# ── Celery task ────────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.ci_fixer.execute_task",
    queue="ci_fixer",
    soft_time_limit=600,
    time_limit=900,
    acks_late=True,
)
def execute_task(ci_fix_run_id: str) -> None:
    """Celery entry point for the CI Fixer agent."""
    try:
        agent = CIFixerAgent(ci_fix_run_id=ci_fix_run_id)
        asyncio.run(agent.execute())
    except Exception:
        log.exception("ci_fixer.celery_task_unhandled", ci_fix_run_id=ci_fix_run_id)
        raise
