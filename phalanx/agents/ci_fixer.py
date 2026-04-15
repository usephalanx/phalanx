"""
CI Fixer Agent — multi-stage pipeline for autonomous CI failure repair.

Pipeline:
  1. Fetch raw CI logs (provider-specific fetcher)
  2. Parse logs deterministically → structured errors (LogParser)
  3. Fingerprint the failure class (stored for V2 history)
  4. Confirm root cause via LLM with windowed file context (RootCauseAnalyst)
  5. Apply fix patches to cloned workspace — line-range replacement only
  6. Validate fix by re-running the failing tool (Validator)
  7. Commit to phalanx/ci-fix/{run_id} branch (NEVER the author's branch)
  8. Open a draft PR targeting the original branch
  9. Comment on the original PR with a link to the fix PR

Phase 1 guard rails:
  - Windowed file context (±40 lines) → LLM cannot rewrite files it doesn't see
  - Line-count delta guard (≤ MAX_LINE_DELTA lines added/removed per patch)
  - Hunk verification: original lines must match before replacement is applied
  - Never pushes to the author's branch
  - Never auto-merges: fix PR is always opened as a DRAFT
  - Low confidence or validation failure → no commit, PR comment explains why
  - Max 2 analyst iterations per run
  - Workspace cleaned up after every run (success or failure)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.ci_fixer.analyst import FilePatch, FixPlan, RootCauseAnalyst
from phalanx.ci_fixer.events import CIFailureEvent
from phalanx.ci_fixer.log_fetcher import get_log_fetcher
from phalanx.ci_fixer.log_parser import ParsedLog, parse_log
from phalanx.ci_fixer.suppressor import is_flaky_suppressed, should_use_history
from phalanx.ci_fixer.validator import validate_fix
from phalanx.ci_fixer.version_parity import (
    VersionParityResult,
    check_version_parity,
    format_parity_notice,
    should_auto_merge,
)
from phalanx.config.settings import get_settings
from phalanx.db.models import CIFailureFingerprint, CIFixRun, CIFlakyPattern, CIIntegration
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)
settings = get_settings()

_MAX_ITERATIONS = 2
# Maximum lines added or removed across ALL patches in a single fix run.
# Mechanical fixes (unused import, line length) change 1–3 lines.
# If the total delta exceeds this the run is aborted.
_MAX_TOTAL_LINE_DELTA = 30
# Maximum number of files a single fix run may touch.
_MAX_FILES_CHANGED = 3


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
            task_id=None,  # CI fixer runs outside the task graph — no task row
        )
        self.ci_fix_run_id = ci_fix_run_id

    async def execute(self) -> AgentResult:
        self._log.info("ci_fixer.execute.start", ci_fix_run_id=self.ci_fix_run_id)
        workspace: Path | None = None

        try:
            return await self._execute_inner()
        except Exception as exc:
            self._log.exception("ci_fixer.execute.unhandled", error=str(exc))
            return AgentResult(success=False, output={}, error=str(exc))
        finally:
            # Always clean up workspace — prevents disk exhaustion
            if workspace is not None:
                _cleanup_workspace(workspace)

    async def _execute_inner(self) -> AgentResult:
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
        fingerprint = _compute_fingerprint(parsed)

        self._log.info(
            "ci_fixer.parsed",
            tool=parsed.tool,
            lint=len(parsed.lint_errors),
            type_=len(parsed.type_errors),
            test=len(parsed.test_failures),
            build=len(parsed.build_errors),
            fingerprint=fingerprint,
            summary=parsed.summary(),
        )

        # Persist fingerprint immediately — even if we can't fix it,
        # the hash is valuable for V2 history queries.
        await self._persist_fingerprint(fingerprint)

        await self._trace(
            "decision",
            f"**Parsed log** — tool: `{parsed.tool}`\n\n{parsed.as_text()}",
            {"tool": parsed.tool, "summary": parsed.summary(), "fingerprint": fingerprint},
        )

        if not parsed.has_errors:
            self._log.warning("ci_fixer.no_structured_errors", raw_preview=raw_log[:300])
            await self._mark_failed(ci_run, "no_structured_errors")
            return AgentResult(
                success=False,
                output={"reason": "no_structured_errors", "raw_preview": raw_log[:500]},
            )

        # ── 3b. Phase 3: Flaky suppressor gate ───────────────────────────────
        flaky_patterns = await self._load_flaky_patterns(ci_run.repo_full_name, parsed)
        if is_flaky_suppressed(parsed, flaky_patterns):
            self._log.info(
                "ci_fixer.flaky_suppressed",
                repo=ci_run.repo_full_name,
                tool=parsed.tool,
                fingerprint=fingerprint,
            )
            await self._mark_failed(ci_run, "flaky_suppressed")
            return AgentResult(
                success=False,
                output={
                    "reason": "flaky_suppressed",
                    "tool": parsed.tool,
                    "fingerprint": fingerprint,
                },
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
            _cleanup_workspace(workspace)
            await self._mark_failed(ci_run, "repo_clone_failed")
            return AgentResult(success=False, output={}, error="repo clone failed")

        # ── 5. Analyst loop: confirm root cause → apply → validate ────────────
        analyst = RootCauseAnalyst(
            call_llm=self._call_claude,
            history_lookup=self._lookup_fix_history,
        )
        fix_plan: FixPlan | None = None
        validation_passed = False
        validation_tool_version = ""
        current_parsed = parsed

        for iteration in range(1, _MAX_ITERATIONS + 1):
            self._log.info("ci_fixer.analyst_iteration", iteration=iteration)

            fix_plan = analyst.analyze(current_parsed, workspace, fingerprint_hash=fingerprint)
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

            # Guard: total line delta across all patches
            total_delta = sum(abs(p.delta) for p in fix_plan.patches)
            if total_delta > _MAX_TOTAL_LINE_DELTA:
                self._log.warning(
                    "ci_fixer.patch_delta_exceeded",
                    total_delta=total_delta,
                    max_allowed=_MAX_TOTAL_LINE_DELTA,
                )
                fix_plan = FixPlan(
                    confidence="low",
                    root_cause=f"Patch too large ({total_delta} lines changed, max {_MAX_TOTAL_LINE_DELTA})",
                )
                break

            # Guard: number of files
            if len(fix_plan.patches) > _MAX_FILES_CHANGED:
                self._log.warning(
                    "ci_fixer.too_many_files",
                    files=len(fix_plan.patches),
                    max_allowed=_MAX_FILES_CHANGED,
                )
                fix_plan = FixPlan(
                    confidence="low",
                    root_cause=f"Fix touches {len(fix_plan.patches)} files (max {_MAX_FILES_CHANGED})",
                )
                break

            # Apply patches
            files_written = self._apply_patches(workspace, fix_plan.patches)
            if not files_written:
                self._log.warning("ci_fixer.no_files_written")
                fix_plan = FixPlan(
                    confidence="low",
                    root_cause="Patch application failed — hunk mismatch or guard rejection",
                )
                break

            # Validate
            validation = validate_fix(current_parsed, workspace, original_parsed=parsed)
            validation_tool_version = validation.tool_version
            self._log.info(
                "ci_fixer.validation",
                passed=validation.passed,
                tool=validation.tool,
                tool_version=validation_tool_version,
                regressions=len(getattr(validation, "regressions", []) or []),
                iteration=iteration,
            )

            if validation.passed:
                validation_passed = True
                self._log.info("ci_fixer.validation_passed", files=files_written)
                break
            else:
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
                if iteration < _MAX_ITERATIONS:
                    # Re-parse the validation output for the next iteration
                    retry_parsed = parse_log(validation.output)
                    if retry_parsed.has_errors:
                        current_parsed = retry_parsed

        # ── 6. Check final plan ───────────────────────────────────────────────
        if not fix_plan or not fix_plan.is_actionable or not validation_passed:
            reason = "low_confidence" if (not fix_plan or not fix_plan.is_actionable) else "validation_failed"
            await self._mark_failed_with_fields(
                ci_run,
                reason=reason,
                fingerprint_hash=fingerprint,
                validation_tool_version=validation_tool_version,
            )
            # Comment on the PR explaining why we couldn't fix it
            if ci_run.pr_number and integration.github_token:
                await self._comment_unable_to_fix(
                    integration=integration,
                    ci_run=ci_run,
                    reason=reason,
                    root_cause=fix_plan.root_cause if fix_plan else "",
                    tool=parsed.tool,
                )
            return AgentResult(
                success=False,
                output={
                    "reason": reason,
                    "root_cause": fix_plan.root_cause if fix_plan else "",
                    "tool": parsed.tool,
                    "fingerprint": fingerprint,
                },
            )

        files_written = [p.path for p in fix_plan.patches]

        # ── 6b. Phase 4: Tool version parity check ────────────────────────────
        # Compare local tool version to the version that caused the failure.
        # We use last_good_tool_version from the fingerprint as the "failure version"
        # proxy (it was the version at the last successful fix — close enough for parity).
        parity_result = await self._check_tool_version_parity(
            fingerprint_hash=fingerprint,
            local_version=validation_tool_version,
        )
        parity_ok = parity_result.ok

        # ── 7. Commit to safe branch (NEVER the author's branch) ─────────────
        fix_branch = f"phalanx/ci-fix/{self.ci_fix_run_id}"
        commit_result = await self._commit_to_safe_branch(
            workspace=workspace,
            source_branch=ci_run.branch,
            fix_branch=fix_branch,
            commit_message=(
                f"fix(ci): resolve {parsed.tool} failure [{ci_run.ci_provider}]\n\n"
                f"Root cause: {fix_plan.root_cause}\n"
                f"Files: {', '.join(files_written)}\n"
                f"Validated: {validation_tool_version}\n"
                f"CI Fix Run: {self.ci_fix_run_id}"
            ),
            github_token=self._get_github_token(integration),
            repo_full_name=ci_run.repo_full_name,
        )
        commit_sha = commit_result.get("sha")
        push_failed = commit_result.get("push_failed", False)

        if not commit_sha:
            await self._mark_failed_with_fields(
                ci_run,
                reason=commit_result.get("error", "commit_failed"),
                fingerprint_hash=fingerprint,
                validation_tool_version=validation_tool_version,
            )
            return AgentResult(success=False, output={}, error="commit failed")

        # ── 8. Open PR (draft or auto-merge depending on integration config) ────
        # Phase 4: auto-merge only if integration.auto_merge=True AND the
        # fingerprint has enough successful fixes AND tool version parity is OK.
        fingerprint_success = await self._get_fingerprint_success_count(fingerprint)
        enable_auto_merge = should_auto_merge(
            integration_auto_merge=getattr(integration, "auto_merge", False),
            fingerprint_success_count=fingerprint_success,
            min_success_count=getattr(integration, "min_success_count", 3),
            parity_ok=parity_ok,
        )

        fix_pr_number: int | None = None
        if not push_failed and integration.github_token:
            fix_pr_number = await self._open_draft_pr(
                integration=integration,
                ci_run=ci_run,
                fix_branch=fix_branch,
                files_written=files_written,
                commit_sha=commit_sha,
                tool=parsed.tool,
                root_cause=fix_plan.root_cause,
                parsed=parsed,
                validation_tool_version=validation_tool_version,
                enable_auto_merge=enable_auto_merge,
                parity_notice=format_parity_notice(parity_result),
            )

        # ── 9. Comment on original PR ─────────────────────────────────────────
        if ci_run.pr_number and integration.github_token:
            await self._comment_on_pr(
                integration=integration,
                ci_run=ci_run,
                files_written=files_written,
                commit_sha=commit_sha,
                tool=parsed.tool,
                root_cause=fix_plan.root_cause,
                parsed=parsed,
                fix_pr_number=fix_pr_number,
                validation_tool_version=validation_tool_version,
            )

        # ── 10. Mark FIXED ────────────────────────────────────────────────────
        async with get_db() as session:
            await session.execute(
                update(CIFixRun)
                .where(CIFixRun.id == self.ci_fix_run_id)
                .values(
                    status="FIXED",
                    fix_commit_sha=commit_sha,
                    fix_branch=fix_branch,
                    fix_pr_number=fix_pr_number,
                    fingerprint_hash=fingerprint,
                    validation_tool_version=validation_tool_version,
                    tool_version_parity_ok=parity_ok,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        # ── Phase 2: Store winning patches in fingerprint table for future reuse
        await self._update_fingerprint_on_success(
            fingerprint_hash=fingerprint,
            patches=fix_plan.patches,
            tool_version=validation_tool_version,
            parsed_log=parsed,
        )

        self._log.info(
            "ci_fixer.execute.done",
            tool=parsed.tool,
            files=files_written,
            commit_sha=commit_sha,
            fix_branch=fix_branch,
            fix_pr_number=fix_pr_number,
            root_cause=fix_plan.root_cause,
            fingerprint=fingerprint,
        )

        return AgentResult(
            success=True,
            output={
                "tool": parsed.tool,
                "root_cause": fix_plan.root_cause,
                "files_fixed": files_written,
                "commit_sha": commit_sha,
                "fix_branch": fix_branch,
                "fix_pr_number": fix_pr_number,
                "confidence": fix_plan.confidence,
                "fingerprint": fingerprint,
                "validation_tool_version": validation_tool_version,
            },
        )

    # ── Log fetching ───────────────────────────────────────────────────────────

    async def _fetch_logs(self, event: CIFailureEvent, integration: CIIntegration) -> str:
        async with get_db() as session:
            ci_run = await self._load_ci_fix_run(session)
        try:
            fetcher = get_log_fetcher(event.provider)
            raw_key = self._decrypt_key(integration.ci_api_key_enc or "")
            api_key = raw_key or self._get_github_token(integration)
            return await fetcher.fetch(event, api_key)
        except Exception as exc:
            self._log.warning("ci_fixer.log_fetch_failed", error=str(exc))
            return ci_run.failure_summary or "(no logs available)" if ci_run else "(no logs)"

    # ── Patch application ──────────────────────────────────────────────────────

    def _apply_patches(self, workspace: Path, patches: list[FilePatch]) -> list[str]:
        """
        Apply line-range patches to files in the workspace.

        For each patch:
          - Verify the original lines match what the analyst was shown
            (fuzzy: strips trailing whitespace for comparison).
          - Replace exactly lines [start_line-1 : end_line] with
            patch.corrected_lines.
          - Abort this patch (log warning, skip) on any mismatch.

        Returns list of relative paths that were successfully written.
        """
        written: list[str] = []
        for patch in patches:
            full_path = workspace / patch.path
            if not full_path.exists():
                self._log.warning("ci_fixer.patch_file_missing", path=patch.path)
                continue

            try:
                original_lines = full_path.read_text(encoding="utf-8").splitlines(
                    keepends=True
                )
            except Exception as exc:
                self._log.warning("ci_fixer.patch_read_failed", path=patch.path, error=str(exc))
                continue

            # Convert to 0-indexed slice
            s = patch.start_line - 1
            e = patch.end_line       # exclusive in Python slice

            # Bounds check
            if s < 0 or e > len(original_lines) or s >= e:
                self._log.warning(
                    "ci_fixer.patch_bounds_invalid",
                    path=patch.path,
                    start=patch.start_line,
                    end=patch.end_line,
                    file_lines=len(original_lines),
                )
                continue

            # Guard: line-count delta on this individual patch
            window_size = e - s
            delta = len(patch.corrected_lines) - window_size
            if abs(delta) > _MAX_TOTAL_LINE_DELTA:
                self._log.warning(
                    "ci_fixer.patch_delta_too_large",
                    path=patch.path,
                    delta=delta,
                    max=_MAX_TOTAL_LINE_DELTA,
                )
                continue

            # Apply
            new_lines = original_lines[:s] + patch.corrected_lines + original_lines[e:]

            try:
                full_path.write_text("".join(new_lines), encoding="utf-8")
                written.append(patch.path)
                self._log.info(
                    "ci_fixer.patch_applied",
                    path=patch.path,
                    lines_delta=delta,
                    start=patch.start_line,
                    end=patch.end_line,
                )
            except Exception as exc:
                self._log.warning("ci_fixer.patch_write_failed", path=patch.path, error=str(exc))

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

    async def _commit_to_safe_branch(
        self,
        workspace: Path,
        source_branch: str,
        fix_branch: str,
        commit_message: str,
        github_token: str,
        repo_full_name: str,
    ) -> dict[str, Any]:
        """
        Create a new branch named fix_branch from source_branch HEAD,
        commit all workspace changes to it, and push to origin.

        NEVER modifies source_branch.
        """
        try:
            from git import Actor, Repo  # noqa: PLC0415
            from git.exc import InvalidGitRepositoryError  # noqa: PLC0415

            try:
                repo = Repo(str(workspace))
            except InvalidGitRepositoryError:
                return {"sha": None, "error": "not a git repo"}

            # Create the safe fix branch from current HEAD (which is source_branch)
            try:
                repo.git.checkout("-b", fix_branch)
            except Exception as exc:
                # Branch may already exist if workspace was reused — reset it
                self._log.warning(
                    "ci_fixer.git.fix_branch_exists",
                    fix_branch=fix_branch,
                    error=str(exc),
                )
                repo.git.checkout(fix_branch)
                repo.git.reset("--hard", f"origin/{source_branch}")

            repo.git.add("-A")

            if not repo.index.diff("HEAD") and not repo.untracked_files:
                return {"sha": None, "message": "no_changes"}

            author = Actor(settings.git_author_name, settings.git_author_email)
            commit = repo.index.commit(commit_message, author=author, committer=author)
            sha = commit.hexsha[:8]
            self._log.info("ci_fixer.git.committed", sha=sha, branch=fix_branch)

            # Push to origin using authenticated URL
            push_failed = False
            if github_token and repo.remotes:
                try:
                    auth_url = (
                        f"https://github.com/{repo_full_name}.git"
                        .replace("https://", f"https://{github_token}@")
                    )
                    repo.git.push(auth_url, f"HEAD:{fix_branch}", "--set-upstream")
                    self._log.info("ci_fixer.git.pushed", branch=fix_branch, sha=sha)
                except Exception as push_exc:
                    self._log.warning("ci_fixer.git.push_failed", error=str(push_exc))
                    push_failed = True

            return {"sha": sha, "branch": fix_branch, "push_failed": push_failed}
        except Exception as exc:
            self._log.warning("ci_fixer.git.commit_failed", error=str(exc))
            return {"sha": None, "error": str(exc)}

    # ── Draft PR creation ──────────────────────────────────────────────────────

    async def _open_draft_pr(
        self,
        integration: CIIntegration,
        ci_run: CIFixRun,
        fix_branch: str,
        files_written: list[str],
        commit_sha: str,
        tool: str,
        root_cause: str,
        parsed: ParsedLog,
        validation_tool_version: str,
        enable_auto_merge: bool = False,
        parity_notice: str = "",
    ) -> int | None:
        """
        Open a PR from fix_branch → ci_run.branch.

        Phase 4: if enable_auto_merge=True, opens a real (non-draft) PR and
        immediately enables GitHub auto-merge (squash).  Only triggered when
        integration.auto_merge=True AND fingerprint is sufficiently proven AND
        tool version parity is OK.

        Default: draft=True (Phase 1 safe behaviour unchanged).
        Returns the new PR number, or None on failure.
        """
        import httpx  # noqa: PLC0415

        files_list = "\n".join(f"- `{f}`" for f in files_written)
        error_detail = _format_error_detail(parsed)
        proof = (
            f"`{validation_tool_version}` exited 0 on the fixed files."
            if validation_tool_version
            else "Validation passed (tool version unknown)."
        )

        original_pr_context = (
            f"Triggered by CI failure on PR #{ci_run.pr_number}."
            if ci_run.pr_number
            else f"Triggered by CI failure on branch `{ci_run.branch}`."
        )

        auto_merge_notice = (
            "\n\n⚡ **Auto-merge enabled** — this PR will be merged automatically "
            "once all required status checks pass."
            if enable_auto_merge
            else ""
        )

        footer = (
            "*Auto-merge is enabled — will merge when all checks pass.*\n"
            if enable_auto_merge
            else
            "*This is a draft PR — Phalanx never auto-merges. "
            "Review the diff above, then mark ready and merge if correct.*\n"
        )

        body = (
            f"## Phalanx CI Fix\n\n"
            f"{original_pr_context}\n\n"
            f"**Root cause:** {root_cause}\n\n"
            f"{error_detail}\n\n"
            f"**Files changed:**\n{files_list}\n\n"
            f"**Validation proof:** {proof}\n\n"
            f"{parity_notice}\n\n"
            f"---\n"
            f"{footer}"
            f"{auto_merge_notice}\n"
            f"*Fix run: `{self.ci_fix_run_id}`*"
        )

        title = f"fix(ci): {tool} — {root_cause[:72]}"
        is_draft = not enable_auto_merge

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"https://api.github.com/repos/{ci_run.repo_full_name}/pulls",
                    headers={
                        "Authorization": f"Bearer {integration.github_token}",
                        "Accept": "application/vnd.github+json",
                    },
                    json={
                        "title": title,
                        "body": body,
                        "head": fix_branch,
                        "base": ci_run.branch,
                        "draft": is_draft,
                    },
                )
            if r.status_code in (200, 201):
                pr_number = r.json()["number"]
                self._log.info(
                    "ci_fixer.pr_opened",
                    pr=pr_number,
                    fix_branch=fix_branch,
                    target=ci_run.branch,
                    draft=is_draft,
                    auto_merge=enable_auto_merge,
                )

                # Phase 4: enable GitHub auto-merge on the PR
                if enable_auto_merge:
                    await self._enable_github_auto_merge(
                        integration=integration,
                        repo_full_name=ci_run.repo_full_name,
                        pr_number=pr_number,
                    )

                return pr_number
            else:
                self._log.warning(
                    "ci_fixer.draft_pr_failed",
                    status=r.status_code,
                    body=r.text[:300],
                )
        except Exception as exc:
            self._log.warning("ci_fixer.draft_pr_error", error=str(exc))

        return None

    async def _enable_github_auto_merge(
        self,
        integration: CIIntegration,
        repo_full_name: str,
        pr_number: int,
    ) -> None:
        """
        Enable auto-merge on a GitHub PR using the GraphQL API.

        GitHub's auto-merge requires:
          1. The PR must not be in draft state
          2. The repo must have auto-merge enabled in settings
          3. At least one branch protection rule with required checks

        Failures are logged but do not abort the fix run — the PR was already
        created successfully.
        """
        import httpx  # noqa: PLC0415

        # GitHub requires GraphQL for enabling auto-merge
        # First, get the PR node_id via REST API
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
                    headers={
                        "Authorization": f"Bearer {integration.github_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if r.status_code != 200:
                self._log.warning("ci_fixer.auto_merge_get_pr_failed", status=r.status_code)
                return

            node_id = r.json().get("node_id")
            if not node_id:
                return

            # Enable auto-merge via GraphQL
            mutation = """
            mutation EnableAutoMerge($prId: ID!) {
              enablePullRequestAutoMerge(input: {pullRequestId: $prId, mergeMethod: SQUASH}) {
                pullRequest {
                  autoMergeRequest { mergeMethod }
                }
              }
            }
            """
            async with httpx.AsyncClient(timeout=15) as client:
                gql_r = await client.post(
                    "https://api.github.com/graphql",
                    headers={
                        "Authorization": f"Bearer {integration.github_token}",
                        "Content-Type": "application/json",
                    },
                    json={"query": mutation, "variables": {"prId": node_id}},
                )

            if gql_r.status_code == 200 and "errors" not in gql_r.json():
                self._log.info("ci_fixer.auto_merge_enabled", pr=pr_number)
            else:
                self._log.warning(
                    "ci_fixer.auto_merge_enable_failed",
                    pr=pr_number,
                    status=gql_r.status_code,
                    body=gql_r.text[:300],
                )

        except Exception as exc:
            self._log.warning("ci_fixer.auto_merge_error", pr=pr_number, error=str(exc))

    # ── PR comment (on original PR) ────────────────────────────────────────────

    async def _comment_on_pr(
        self,
        integration: CIIntegration,
        ci_run: CIFixRun,
        files_written: list[str],
        commit_sha: str,
        tool: str,
        root_cause: str,
        parsed: ParsedLog,
        fix_pr_number: int | None,
        validation_tool_version: str,
    ) -> None:
        import httpx  # noqa: PLC0415

        if fix_pr_number:
            fix_ref = (
                f"**Draft fix PR:** #{fix_pr_number} — "
                f"review the diff and merge when satisfied.\n\n"
                f"Phalanx never auto-merges. You decide."
            )
        else:
            fix_ref = (
                f"Fix committed to branch `phalanx/ci-fix/{self.ci_fix_run_id}` "
                f"(commit `{commit_sha}`). Push to origin succeeded but PR creation failed — "
                f"please open a PR manually from that branch."
            )

        error_detail = _format_error_detail(parsed)
        proof = (
            f"`{validation_tool_version}` exited 0"
            if validation_tool_version
            else "validation passed"
        )

        body = (
            f"🔧 **Phalanx CI Fixer** found a fix for the `{tool}` failure.\n\n"
            f"**Root cause:** {root_cause}\n\n"
            f"{error_detail}\n\n"
            f"**Validation:** {proof}\n\n"
            f"{fix_ref}"
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

    async def _comment_unable_to_fix(
        self,
        integration: CIIntegration,
        ci_run: CIFixRun,
        reason: str,
        root_cause: str,
        tool: str,
    ) -> None:
        """Post a comment on the PR explaining why Phalanx couldn't fix it."""
        import httpx  # noqa: PLC0415

        reason_text = {
            "low_confidence": (
                "The failure was parsed successfully but the fix requires "
                "semantic understanding beyond mechanical repair. Manual fix needed."
            ),
            "validation_failed": (
                "A fix was generated but failed local validation — "
                "it would not have made CI green. Aborting to avoid a bad commit."
            ),
        }.get(reason, f"Reason: `{reason}`")

        body = (
            f"🔍 **Phalanx CI Fixer** investigated the `{tool}` failure "
            f"but could not produce a safe fix.\n\n"
            f"**Diagnosed root cause:** {root_cause or '(could not determine)'}\n\n"
            f"{reason_text}\n\n"
            f"*No code was committed. Fix run: `{self.ci_fix_run_id}`*"
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
            self._log.info("ci_fixer.pr_commented_unable", pr=ci_run.pr_number)
        except Exception as exc:
            self._log.warning("ci_fixer.pr_comment_unable_failed", error=str(exc))

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _load_ci_fix_run(self, session) -> CIFixRun | None:
        result = await session.execute(
            select(CIFixRun).where(CIFixRun.id == self.ci_fix_run_id)
        )
        return result.scalar_one_or_none()

    async def _load_integration(self, session, integration_id: str) -> CIIntegration | None:
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.id == integration_id)
        )
        return result.scalar_one_or_none()

    async def _mark_failed(self, ci_run: CIFixRun, reason: str) -> None:
        await self._mark_failed_with_fields(ci_run, reason=reason)

    async def _mark_failed_with_fields(
        self,
        ci_run: CIFixRun,
        reason: str,
        fingerprint_hash: str | None = None,
        validation_tool_version: str | None = None,
    ) -> None:
        values: dict = {
            "status": "FAILED",
            "error": reason,
            "completed_at": datetime.now(UTC),
        }
        if fingerprint_hash:
            values["fingerprint_hash"] = fingerprint_hash
        if validation_tool_version:
            values["validation_tool_version"] = validation_tool_version

        async with get_db() as session:
            await session.execute(
                update(CIFixRun).where(CIFixRun.id == self.ci_fix_run_id).values(**values)
            )
            await session.commit()

    async def _check_tool_version_parity(
        self,
        fingerprint_hash: str | None,
        local_version: str,
    ) -> VersionParityResult:
        """
        Phase 4: Compare local tool version to the version at the last successful fix.

        Uses last_good_tool_version from CIFailureFingerprint as the "known good" baseline.
        If no history exists (first fix), returns ok=True (nothing to compare against).
        """
        if not fingerprint_hash or not local_version:
            return VersionParityResult(
                ok=True,
                local_version=local_version,
                failure_version="",
                reason="no history — parity check skipped",
            )

        try:

            async with get_db() as session:
                result = await session.execute(
                    select(CIFailureFingerprint).where(
                        CIFailureFingerprint.fingerprint_hash == fingerprint_hash
                    )
                )
                fp = result.scalar_one_or_none()

            if fp is None or not fp.last_good_tool_version:
                return VersionParityResult(
                    ok=True,
                    local_version=local_version,
                    failure_version="",
                    reason="no previous fix — parity check skipped",
                )

            return check_version_parity(local_version, fp.last_good_tool_version)

        except Exception as exc:
            self._log.warning("ci_fixer.parity_check_failed", error=str(exc))
            return VersionParityResult(
                ok=True,
                local_version=local_version,
                failure_version="",
                reason=f"parity check failed: {exc}",
            )

    async def _get_fingerprint_success_count(self, fingerprint_hash: str | None) -> int:
        """Phase 4: Return success_count for a fingerprint (0 if not found)."""
        if not fingerprint_hash:
            return 0

        try:
            async with get_db() as session:
                result = await session.execute(
                    select(CIFailureFingerprint).where(
                        CIFailureFingerprint.fingerprint_hash == fingerprint_hash
                    )
                )
                fp = result.scalar_one_or_none()
                return fp.success_count if fp else 0
        except Exception as exc:
            self._log.warning("ci_fixer.fingerprint_count_failed", error=str(exc))
            return 0

    async def _load_flaky_patterns(
        self,
        repo_full_name: str,
        parsed_log: ParsedLog,
    ) -> list[CIFlakyPattern]:
        """
        Phase 3: Load CIFlakyPattern rows matching the errors in parsed_log.

        Returns empty list on any error — suppressor will be a no-op when
        patterns can't be loaded (safe fail-open behaviour).
        """
        if not parsed_log.lint_errors and not parsed_log.type_errors:
            return []

        try:
            from sqlalchemy import and_, or_  # noqa: PLC0415

            # Collect (file, code) pairs from the parsed errors
            error_keys = [
                (e.file, e.code) for e in parsed_log.lint_errors
            ] + [
                (e.file, getattr(e, "code", None)) for e in parsed_log.type_errors
            ]

            if not error_keys:
                return []

            # Build an OR filter for all error keys
            conditions = [
                and_(
                    CIFlakyPattern.error_file == file,
                    CIFlakyPattern.error_code == code,
                )
                for file, code in error_keys
            ]

            async with get_db() as session:
                result = await session.execute(
                    select(CIFlakyPattern).where(
                        CIFlakyPattern.repo_full_name == repo_full_name,
                        or_(*conditions),
                    )
                )
                return list(result.scalars().all())

        except Exception as exc:
            self._log.warning("ci_fixer.flaky_patterns_load_failed", error=str(exc))
            return []

    async def _persist_fingerprint(self, fingerprint_hash: str) -> None:
        """Store fingerprint_hash on the run immediately after parsing."""
        try:
            async with get_db() as session:
                await session.execute(
                    update(CIFixRun)
                    .where(CIFixRun.id == self.ci_fix_run_id)
                    .values(fingerprint_hash=fingerprint_hash)
                )
                await session.commit()
        except Exception as exc:
            self._log.warning("ci_fixer.fingerprint_persist_failed", error=str(exc))

    def _lookup_fix_history(self, fingerprint_hash: str) -> list[dict] | None:
        """
        Synchronous history lookup — returns previously-successful patch dicts
        for this fingerprint, or None if no successful history exists.

        Synchronous because RootCauseAnalyst.analyze() is synchronous
        (Anthropic SDK _call_claude is synchronous).  We run the async DB call
        in a new event loop via asyncio.run() to stay compatible.
        """
        try:
            return asyncio.run(self._async_lookup_fix_history(fingerprint_hash))
        except Exception as exc:
            self._log.warning("ci_fixer.history_lookup_failed", error=str(exc))
            return None

    async def _async_lookup_fix_history(self, fingerprint_hash: str) -> list[dict] | None:
        """Async body of _lookup_fix_history."""
        from sqlalchemy import and_  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(
                select(CIFailureFingerprint).where(
                    and_(
                        CIFailureFingerprint.fingerprint_hash == fingerprint_hash,
                        CIFailureFingerprint.success_count > 0,
                        CIFailureFingerprint.last_good_patch_json.isnot(None),
                    )
                )
            )
            fp = result.scalar_one_or_none()

        if fp is None:
            return None

        # Phase 3: history weighting — only reuse if more successes than failures
        if not should_use_history(fp):
            self._log.debug(
                "ci_fixer.history_lookup_skipped_unreliable",
                fingerprint=fingerprint_hash,
                success=fp.success_count,
                failure=fp.failure_count,
            )
            return None

        try:
            patches = json.loads(fp.last_good_patch_json)
            if isinstance(patches, list) and patches:
                self._log.info(
                    "ci_fixer.history_lookup_hit",
                    fingerprint=fingerprint_hash,
                    patches=len(patches),
                    successes=fp.success_count,
                )
                return patches
        except (json.JSONDecodeError, TypeError) as exc:
            self._log.warning("ci_fixer.history_patch_corrupt", error=str(exc))

        return None

    async def _update_fingerprint_on_success(
        self,
        fingerprint_hash: str,
        patches: list[FilePatch],
        tool_version: str,
        parsed_log: ParsedLog,
    ) -> None:
        """
        After a successful fix is validated, upsert CIFailureFingerprint with
        the winning patches stored as JSON for future history-based reuse.

        Uses INSERT ... ON CONFLICT (via SQLAlchemy upsert) keyed on
        (fingerprint_hash, repo_full_name).
        """
        import dataclasses  # noqa: PLC0415

        try:
            async with get_db() as session:
                result = await session.execute(
                    select(CIFixRun).where(CIFixRun.id == self.ci_fix_run_id)
                )
                run = result.scalar_one_or_none()
                if run is None:
                    return

                repo = run.repo_full_name

                # Serialise patches to JSON (dataclasses don't auto-serialise)
                patch_dicts = [dataclasses.asdict(p) for p in patches]
                patch_json = json.dumps(patch_dicts)

                result = await session.execute(
                    select(CIFailureFingerprint).where(
                        CIFailureFingerprint.fingerprint_hash == fingerprint_hash,
                        CIFailureFingerprint.repo_full_name == repo,
                    )
                )
                fp = result.scalar_one_or_none()

                now = datetime.now(UTC)
                if fp is None:
                    fp = CIFailureFingerprint(
                        id=str(__import__("uuid").uuid4()),
                        fingerprint_hash=fingerprint_hash,
                        repo_full_name=repo,
                        tool=parsed_log.tool,
                        sample_errors=parsed_log.summary(),
                        seen_count=1,
                        success_count=1,
                        failure_count=0,
                        last_good_patch_json=patch_json,
                        last_good_tool_version=tool_version,
                        last_seen_at=now,
                    )
                    session.add(fp)
                else:
                    fp.success_count += 1
                    fp.seen_count += 1
                    fp.last_good_patch_json = patch_json
                    fp.last_good_tool_version = tool_version
                    fp.last_seen_at = now

                await session.commit()
                self._log.info(
                    "ci_fixer.fingerprint_updated",
                    fingerprint=fingerprint_hash,
                    success_count=fp.success_count,
                )
        except Exception as exc:
            self._log.warning("ci_fixer.fingerprint_update_failed", error=str(exc))

    # ── Auth helpers ────────────────────────────────────────────────────────────

    def _decrypt_key(self, encrypted_key: str) -> str:
        return encrypted_key  # Phase 2: KMS decrypt

    def _get_github_token(self, integration: CIIntegration) -> str:
        return integration.github_token or settings.github_token

    # ── Backward-compat shims (used by unit tests) ─────────────────────────────

    def _apply_fix_files(self, workspace: Path, files: list[dict]) -> list[str]:
        """Dict-based shim for unit tests."""
        results: list[str] = []
        for f in files:
            path = f.get("path", "")
            content = f.get("content", "")
            if not path or not content:
                continue
            full = workspace / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
            results.append(path)
        return results

    def _read_files(self, workspace: Path, paths: list[str]) -> str:
        """Shim for unit tests."""
        analyst = RootCauseAnalyst(call_llm=lambda **_: "")
        return analyst._read_files(workspace, paths)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _compute_fingerprint(parsed: ParsedLog) -> str:
    """
    Stable sha256[:16] identity for a failure class.
    Strips run-specific values (line numbers, actual values, paths prefixes).
    Identical failure class on different commits → same hash.
    """
    import re as _re  # noqa: PLC0415

    def _norm_path(p: str) -> str:
        return _re.sub(r"^(\./|/[^\s]+/work/[^\s]+/[^\s]+/)", "", p)

    def _norm_msg(m: str) -> str:
        m = _re.sub(r"'[^']{1,60}'", "'?'", m)
        m = _re.sub(r'"[^"]{1,60}"', '"?"', m)
        m = _re.sub(r"\b\d+\b", "N", m)
        return m.strip()

    def _norm_test_id(t: str) -> str:
        return _re.sub(r"\[.*?\]$", "[?]", t)

    features: list[str] = []
    for e in parsed.lint_errors:
        features.append(f"lint:{_norm_path(e.file)}:{e.code}:{_norm_msg(e.message)}")
    for e in parsed.type_errors:
        features.append(f"type:{_norm_path(e.file)}:{_norm_msg(e.message)}")
    for e in parsed.test_failures:
        features.append(f"test:{_norm_path(e.file)}:{_norm_test_id(e.test_id)}")
    for e in parsed.build_errors:
        features.append(f"build:{_norm_msg(e.message)}")

    canonical = json.dumps(sorted(features))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _format_error_detail(parsed: ParsedLog) -> str:
    lines: list[str] = []
    if parsed.lint_errors:
        lines.append("**Errors fixed:**")
        for e in parsed.lint_errors[:5]:
            lines.append(f"- `{e.file}:{e.line}` — `{e.code}` {e.message}")
    elif parsed.type_errors:
        lines.append("**Type errors fixed:**")
        for e in parsed.type_errors[:5]:
            lines.append(f"- `{e.file}:{e.line}` — {e.message}")
    elif parsed.test_failures:
        lines.append("**Test failures addressed:**")
        for f in parsed.test_failures[:5]:
            lines.append(f"- `{f.test_id}`")
    return "\n".join(lines)


def _cleanup_workspace(workspace: Path) -> None:
    """Remove workspace directory after a run completes or fails."""
    try:
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
            log.debug("ci_fixer.workspace_cleaned", path=str(workspace))
    except Exception as exc:
        log.warning("ci_fixer.workspace_cleanup_failed", path=str(workspace), error=str(exc))


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
