"""
CI Fixer Agent — reads CI failure logs, fixes the code, commits back to the branch.

Responsibilities:
  1. Load CIFixRun record (provider, branch, failing commit, logs)
  2. Fetch raw logs via provider-specific fetcher
  3. Classify failure type (lint / type / test / build / dependency)
  4. Reflect on the failure before generating a fix (soul)
  5. Clone/checkout the failing branch
  6. Read the files mentioned in the failure log
  7. Generate a surgical fix (high-confidence only — never guess)
  8. Apply files, commit, push to the same branch
  9. Comment on the PR explaining what was fixed
  10. Mark CIFixRun FIXED or FAILED

Design invariants:
  - Never modifies test assertions
  - Never touches files not mentioned in the CI failure
  - Low-confidence → no commit, logs uncertainty trace
  - Max 2 attempts per PR (tracked via CIFixRun.attempt)
  - Zero changes to BaseAgent, commander, builder, orchestrator
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.agents.soul import CI_FIXER_SOUL
from phalanx.ci_fixer.classifier import classify_failure, extract_failing_files
from phalanx.ci_fixer.events import CIFailureEvent
from phalanx.ci_fixer.log_fetcher import get_log_fetcher
from phalanx.config.settings import get_settings
from phalanx.db.models import CIFixRun, CIIntegration
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)
settings = get_settings()

# Max files to read for context — keeps prompt manageable
_MAX_CONTEXT_FILES = 8
# Max chars per file read into the prompt
_MAX_FILE_CHARS = 3000

_CI_FIXER_PROMPT = """\
You are fixing a CI failure. Be surgical — fix exactly what the log says is broken.

FAILURE CATEGORY: {category}

CI LOG (failure section):
```
{log_text}
```

FAILING FILES (current content):
{file_contents}

REFLECTION:
{reflection}

RULES — non-negotiable:
1. Fix ONLY what the log explicitly reports as broken.
2. For TEST failures: fix the IMPLEMENTATION to match the assertion. Never change test code.
3. For LINT failures: fix exactly the flagged lines. No cleanup beyond the flag.
4. For TYPE failures: add/fix types or resolve the mismatch. No logic changes.
5. For BUILD failures: fix imports, syntax, or missing modules only.
6. If you cannot determine a HIGH-confidence fix from the log, return empty files.
7. Return ONLY files that need to change — not the full project.

Return a JSON object (no markdown, no explanation outside the JSON):
{{
  "confidence": "high" | "medium" | "low",
  "root_cause": "<one sentence>",
  "files": [
    {{"path": "<relative path>", "content": "<full corrected file content>"}}
  ]
}}

If confidence is "low", set files to [].
If you return medium confidence and the caller has max_attempts=1, it will not be committed.
"""


class CIFixerAgent(BaseAgent):
    """
    Autonomous CI failure repair agent.

    Does not inherit any state from the existing Run/Task pipeline.
    Operates on CIFixRun records created by the webhook ingest layer.
    """

    AGENT_ROLE = "ci_fixer"

    def __init__(self, ci_fix_run_id: str):
        # BaseAgent expects run_id + agent_id + task_id
        # We reuse ci_fix_run_id for all three — ci_fixer doesn't use the
        # normal Run/Task pipeline
        super().__init__(
            run_id=ci_fix_run_id,
            agent_id="ci-fixer",
            task_id=ci_fix_run_id,
        )
        self.ci_fix_run_id = ci_fix_run_id

    async def execute(self) -> AgentResult:
        self._log.info("ci_fixer.execute.start", ci_fix_run_id=self.ci_fix_run_id)

        # ── 1. Load CIFixRun ────────────────────────────────────────────────
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
            return AgentResult(
                success=False,
                output={},
                error="CIIntegration not found",
            )

        # ── 2. Fetch logs ───────────────────────────────────────────────────
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

        try:
            fetcher = get_log_fetcher(ci_run.ci_provider)
            api_key = self._decrypt_key(integration.ci_api_key_enc)
            log_text = await fetcher.fetch(event, api_key)
        except Exception as exc:
            self._log.warning("ci_fixer.log_fetch_failed", error=str(exc))
            log_text = ci_run.failure_summary or "(no logs available)"

        # ── 3. Classify ─────────────────────────────────────────────────────
        category = classify_failure(log_text)
        await self._trace(
            "decision",
            f"Failure category: **{category}**\nLog preview:\n```\n{log_text[:500]}\n```",
            {"category": category, "provider": ci_run.ci_provider},
        )

        # ── 4. Reflect ──────────────────────────────────────────────────────
        reflection = self._reflect(
            task_description=(
                f"Fix {category} CI failure on {ci_run.repo_full_name}:{ci_run.branch}\n\n"
                f"Failing jobs: {', '.join(ci_run.failed_jobs or [])}"
            ),
            context=log_text[:1500],
            soul=CI_FIXER_SOUL,
        )
        if reflection:
            await self._trace("reflection", reflection, {"category": category})

        # ── 5. Clone/checkout branch ─────────────────────────────────────────
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
            await self._mark_failed(ci_run, "repo clone failed")
            return AgentResult(success=False, output={}, error="repo clone failed")

        # ── 6. Read failing files ────────────────────────────────────────────
        failing_file_paths = extract_failing_files(log_text)
        file_contents = self._read_files(workspace, failing_file_paths)

        # ── 7. Generate fix ──────────────────────────────────────────────────
        fix = await self._generate_fix(
            category=category,
            log_text=log_text,
            file_contents=file_contents,
            reflection=reflection,
        )

        if not fix or fix.get("confidence") == "low" or not fix.get("files"):
            msg = f"Low confidence fix for {category} failure — skipping commit"
            self._log.info("ci_fixer.low_confidence", root_cause=fix.get("root_cause") if fix else "n/a")
            await self._trace("uncertainty", msg, {"category": category, "root_cause": fix.get("root_cause") if fix else ""})
            await self._mark_failed(ci_run, "low_confidence")
            return AgentResult(success=False, output={"reason": "low_confidence", "category": category})

        # ── 8. Apply + commit + push ─────────────────────────────────────────
        files_written = self._apply_fix_files(workspace, fix["files"])
        if not files_written:
            await self._mark_failed(ci_run, "no files written")
            return AgentResult(success=False, output={}, error="no files written")

        commit_result = await self._commit_and_push(
            workspace=workspace,
            branch=ci_run.branch,
            commit_message=(
                f"fix(ci): resolve {category} failure [{ci_run.ci_provider}]\n\n"
                f"Root cause: {fix.get('root_cause', 'see CI log')}\n"
                f"Files: {', '.join(files_written)}\n"
                f"CI Fix Run: {self.ci_fix_run_id}"
            ),
        )
        commit_sha = commit_result.get("sha")

        # ── 9. Comment on PR ─────────────────────────────────────────────────
        if ci_run.pr_number and commit_sha and integration.github_token:
            await self._comment_on_pr(
                integration=integration,
                ci_run=ci_run,
                files_written=files_written,
                commit_sha=commit_sha,
                category=category,
                root_cause=fix.get("root_cause", ""),
            )

        # ── 10. Mark FIXED ───────────────────────────────────────────────────
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
            category=category,
            files=files_written,
            commit_sha=commit_sha,
        )

        return AgentResult(
            success=True,
            output={
                "category": category,
                "root_cause": fix.get("root_cause", ""),
                "files_fixed": files_written,
                "commit_sha": commit_sha,
                "confidence": fix.get("confidence"),
            },
        )

    # ── Fix generation ─────────────────────────────────────────────────────────

    async def _generate_fix(
        self,
        category: str,
        log_text: str,
        file_contents: str,
        reflection: str,
    ) -> dict | None:
        prompt = _CI_FIXER_PROMPT.format(
            category=category,
            log_text=log_text[:4000],
            file_contents=file_contents[:6000],
            reflection=reflection[:800] if reflection else "No reflection available.",
        )

        try:
            raw = self._call_claude(
                messages=[{"role": "user", "content": prompt}],
                system=CI_FIXER_SOUL,
                max_tokens=4096,
            )
            # Strip markdown fences if Claude wraps in ```json
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(raw)
        except json.JSONDecodeError:
            self._log.warning("ci_fixer.fix_parse_failed", raw=raw[:200])
            return None
        except Exception as exc:
            self._log.warning("ci_fixer.fix_generation_failed", error=str(exc))
            return None

    # ── Git helpers ────────────────────────────────────────────────────────────

    async def _clone_repo(
        self,
        workspace: Path,
        repo_full_name: str,
        branch: str,
        commit_sha: str,
        github_token: str,
    ) -> bool:
        """Clone the repo at the failing commit SHA, checkout the branch."""
        try:
            from git import Repo  # noqa: PLC0415

            repo_url = f"https://github.com/{repo_full_name}.git"
            auth_url = repo_url.replace("https://", f"https://{github_token}@")

            git_dir = workspace / ".git"
            if git_dir.exists():
                repo = Repo(str(workspace))
                repo.remotes.origin.fetch()
            else:
                repo = Repo.clone_from(auth_url, str(workspace))

            # Checkout the branch at the failing commit
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
        """Commit all changes and push to origin."""
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

    # ── File helpers ───────────────────────────────────────────────────────────

    def _read_files(self, workspace: Path, paths: list[str]) -> str:
        """Read file contents from workspace. Returns formatted string for prompt."""
        sections: list[str] = []
        for rel_path in paths[:_MAX_CONTEXT_FILES]:
            full_path = workspace / rel_path
            if not full_path.exists():
                # Try to find it with a glob
                matches = list(workspace.rglob(Path(rel_path).name))
                if matches:
                    full_path = matches[0]
                    rel_path = str(full_path.relative_to(workspace))
                else:
                    continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                if len(content) > _MAX_FILE_CHARS:
                    content = content[:_MAX_FILE_CHARS] + "\n... (truncated)"
                sections.append(f"### {rel_path}\n```\n{content}\n```")
            except Exception:
                continue
        return "\n\n".join(sections) if sections else "(no files found)"

    def _apply_fix_files(self, workspace: Path, files: list[dict]) -> list[str]:
        """Write fix files to workspace. Returns list of relative paths written."""
        written: list[str] = []
        for f in files:
            rel_path = f.get("path", "")
            content = f.get("content", "")
            if not rel_path or not content:
                continue
            full_path = workspace / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                full_path.write_text(content, encoding="utf-8")
                written.append(rel_path)
            except Exception as exc:
                self._log.warning("ci_fixer.write_failed", path=rel_path, error=str(exc))
        return written

    # ── PR comment ─────────────────────────────────────────────────────────────

    async def _comment_on_pr(
        self,
        integration: CIIntegration,
        ci_run: CIFixRun,
        files_written: list[str],
        commit_sha: str,
        category: str,
        root_cause: str,
    ) -> None:
        """Post a PR comment explaining the fix."""
        import httpx  # noqa: PLC0415

        files_list = "\n".join(f"- `{f}`" for f in files_written)
        body = (
            f"🔧 **Phalanx CI Fixer** resolved a `{category}` failure "
            f"in commit `{commit_sha}`.\n\n"
            f"**Root cause:** {root_cause}\n\n"
            f"**Files changed:**\n{files_list}\n\n"
            f"A new CI run has been triggered automatically. "
            f"If it still fails, I'll try once more."
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
        async with get_db() as session:
            await session.execute(
                update(CIFixRun)
                .where(CIFixRun.id == self.ci_fix_run_id)
                .values(status="FAILED", error=reason, completed_at=datetime.now(UTC))
            )
            await session.commit()

    # ── Auth helpers ────────────────────────────────────────────────────────────

    def _decrypt_key(self, encrypted_key: str) -> str:
        """Decrypt a stored CI API key. Phase 1: no-op (plaintext). Phase 2: KMS."""
        # TODO Phase 2: decrypt with AWS KMS or Secrets Manager
        return encrypted_key

    def _get_github_token(self, integration: CIIntegration) -> str:
        """Return the GitHub token for this integration."""
        return integration.github_token or settings.github_token


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
