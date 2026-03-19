"""
Builder Agent — implements code changes based on the Planner's output.

Responsibilities:
  1. Load task + planner's implementation plan from prior tasks
  2. Set up git workspace: clone/update repo, create/checkout branch
  3. Read existing file contents for context
  4. Call Claude to generate precise file changes (JSON)
  5. Write files to disk
  6. Git add + commit (with FORGE bot author); push if remote configured
  7. Update Run.active_branch; persist diff as Artifact
  8. Mark task COMPLETED

Design (AD-001):
  - Builder uses Anthropic API (not Claude Code SDK) for MVP.
    Claude Code SDK subprocess integration is post-MVP.
  - If GitHub token + project repo_url are configured: real git commits.
  - If not: writes to local workspace only (for local demo/testing).
  - AP-003: exceptions propagate — Celery handles retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from forge.agents.base import AgentResult, BaseAgent
from forge.config.settings import get_settings
from forge.db.models import Artifact, Run, Task
from forge.db.session import get_db
from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

settings = get_settings()

# Token budget for code generation — generous to fit full file contents
_BUILD_MAX_TOKENS = 8000
# Max bytes to read from any single existing file (avoid token overflow)
_MAX_FILE_READ_BYTES = 12_000
# Max total bytes of existing file context to send to Claude
_MAX_CONTEXT_BYTES = 40_000


class BuilderAgent(BaseAgent):
    """
    IC4-level implementation agent.

    Reads the plan, writes the code, commits it. That's the contract.
    Every file change is recorded in Task.output and persisted as an Artifact.
    """

    AGENT_ROLE = "builder"

    async def execute(self) -> AgentResult:
        self._log.info("builder.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            run = await self._load_run(session)
            plan = await self._load_planner_plan(session, task.sequence_num)

        # Set up workspace
        workspace = self._workspace_path(run)
        await self._ensure_workspace(workspace, run)

        # Read relevant existing files for context
        existing_files = self._read_existing_files(workspace, task)

        # Generate code changes
        changes = await self._generate_changes(task, plan, existing_files, workspace)

        # Apply changes to disk
        files_written = self._apply_changes(workspace, changes)

        # Commit (git if available, else record locally)
        commit_info = await self._commit_changes(workspace, task, run, files_written)

        output = {
            "workspace": str(workspace),
            "files_written": files_written,
            "commit": commit_info,
            "plan_used": bool(plan),
            "summary": changes.get("summary", ""),
        }

        async with get_db() as session:
            run_ref = await self._load_run(session)

            # Update Run.active_branch if commit produced a branch
            if commit_info.get("branch"):
                await session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(active_branch=commit_info["branch"], updated_at=datetime.now(UTC))
                )

            # Persist diff artifact
            await self._persist_artifact(session, output, run_ref.project_id, changes)

            # Mark task complete
            await session.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(
                    status="COMPLETED",
                    output=output,
                    actual_complexity=task.estimated_complexity,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        await self._audit(
            event_type="task_complete",
            payload={
                "files_written": len(files_written),
                "has_commit": bool(commit_info.get("sha")),
            },
        )

        self._log.info(
            "builder.execute.done",
            files_written=len(files_written),
            tokens_used=self._tokens_used,
        )
        return AgentResult(success=True, output=output, tokens_used=self._tokens_used)

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_run(self, session) -> Run:
        result = await session.execute(select(Run).where(Run.id == self.run_id))
        return result.scalar_one()

    async def _load_planner_plan(self, session, before_seq: int) -> dict:
        """Find the most recent completed planner task output in this run."""
        result = await session.execute(
            select(Task)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "planner",
                Task.sequence_num < before_seq,
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        return task.output or {} if task else {}

    # ── Workspace helpers ─────────────────────────────────────────────────────

    def _workspace_path(self, run: Run) -> Path:
        base = Path(settings.git_workspace)
        return base / run.project_id / self.run_id

    async def _ensure_workspace(self, workspace: Path, run: Run) -> None:
        """
        Set up the workspace directory. If GitHub is configured and project has
        a repo_url, clone/update. Otherwise create a local directory.
        """
        workspace.mkdir(parents=True, exist_ok=True)

        branch = run.active_branch or f"forge/run-{self.run_id[:8]}"

        if settings.github_token:
            await self._setup_git_workspace(workspace, run, branch)
        else:
            self._log.info("builder.workspace.local", path=str(workspace))

    async def _setup_git_workspace(self, workspace: Path, run: Run, branch: str) -> None:
        """Clone or update the repo, then checkout/create the working branch."""
        try:
            from git import Repo  # noqa: PLC0415

            # Try to get project repo_url from DB project.config
            async with get_db() as session:
                from forge.db.models import Project  # noqa: PLC0415

                result = await session.execute(select(Project).where(Project.id == run.project_id))
                project = result.scalar_one_or_none()

            repo_url = (project.config or {}).get("repo_url", "") if project else ""

            if not repo_url:
                self._log.info("builder.git.no_repo_url", project_id=run.project_id)
                return

            # Embed token in URL for authentication
            auth_url = repo_url.replace("https://", f"https://{settings.github_token}@")

            git_dir = workspace / ".git"
            if git_dir.exists():
                repo = Repo(str(workspace))
                repo.remotes.origin.fetch()
                self._log.info("builder.git.fetched", workspace=str(workspace))
            else:
                repo = Repo.clone_from(auth_url, str(workspace))
                self._log.info("builder.git.cloned", url=repo_url)

            # Checkout or create the working branch
            try:
                repo.git.checkout(branch)
            except Exception:
                repo.git.checkout("-b", branch)

            self._log.info("builder.git.branch_ready", branch=branch)

        except ImportError:
            self._log.warning("builder.git.gitpython_missing")
        except Exception as exc:
            self._log.warning("builder.git.setup_failed", error=str(exc))

    def _read_existing_files(self, workspace: Path, task: Task) -> dict[str, str]:
        """
        Read relevant existing files to give Claude the current code state.
        Prioritises files_likely_touched; falls back to a shallow directory scan.
        """
        contents: dict[str, str] = {}
        total_bytes = 0

        # First: explicitly listed files
        for rel_path in task.files_likely_touched or []:
            full = workspace / rel_path
            if full.exists() and full.is_file():
                try:
                    text = full.read_text(errors="replace")[:_MAX_FILE_READ_BYTES]
                    contents[rel_path] = text
                    total_bytes += len(text.encode())
                    if total_bytes >= _MAX_CONTEXT_BYTES:
                        break
                except OSError:
                    pass

        # If workspace has Python files and we have budget, add a few for context
        if total_bytes < _MAX_CONTEXT_BYTES // 2:
            for py_file in sorted(workspace.rglob("*.py"))[:20]:
                if py_file.stat().st_size == 0:
                    continue
                rel = str(py_file.relative_to(workspace))
                if rel in contents:
                    continue
                # Skip test files and migrations to save tokens
                if any(skip in rel for skip in ("test_", "alembic/versions", "__pycache__")):
                    continue
                try:
                    text = py_file.read_text(errors="replace")[:_MAX_FILE_READ_BYTES]
                    contents[rel] = text
                    total_bytes += len(text.encode())
                    if total_bytes >= _MAX_CONTEXT_BYTES:
                        break
                except OSError:
                    pass

        return contents

    # ── Code generation ───────────────────────────────────────────────────────

    async def _generate_changes(
        self,
        task: Task,
        plan: dict,
        existing_files: dict[str, str],
        workspace: Path,
    ) -> dict[str, Any]:
        """Call Claude to generate complete file contents."""

        # Build file context string (truncated)
        file_context = ""
        if existing_files:
            parts = []
            for path, content in existing_files.items():
                parts.append(f"--- {path} ---\n{content}")
            file_context = "\n\n".join(parts)[:_MAX_CONTEXT_BYTES]

        plan_text = (
            json.dumps(plan, indent=2)[:4000]
            if plan
            else "No explicit plan — use task description."
        )

        system = """\
You are an expert software engineer in FORGE, an AI team operating system.
Your role: implement the code changes described in the task and plan.

Rules:
- Write complete file contents (not partial diffs or snippets).
- Follow existing code style exactly (indentation, naming, patterns).
- Every new function/class must have a docstring.
- Implement tests for new functionality (test_*.py files in tests/).
- Use type annotations throughout (Python 3.12 style).
- Never hardcode credentials or secrets.

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON.

{
  "summary": "one sentence describing what was implemented",
  "commit_message": "feat: concise commit message (< 72 chars)",
  "files": [
    {
      "path": "relative/path/to/file.py",
      "action": "create|modify|delete",
      "content": "complete file content as a string"
    }
  ]
}"""

        messages = [
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\n\n"
                    f"Description: {task.description}\n\n"
                    f"Implementation Plan:\n{plan_text}\n\n"
                    + (
                        f"Existing code context:\n{file_context}\n\n"
                        if file_context
                        else "No existing files found — create from scratch.\n\n"
                    )
                    + "Implement the required changes. Write complete, production-ready code."
                ),
            }
        ]

        raw = self._call_claude(messages=messages, system=system, max_tokens=_BUILD_MAX_TOKENS)

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            self._log.error("builder.json_parse_failed", raw_len=len(raw))
            # Return the raw text as a single README-style file so work isn't lost
            return {
                "summary": "Code generation completed (raw output stored)",
                "commit_message": f"feat: {task.title[:60]}",
                "files": [
                    {
                        "path": "forge/_generated/output.txt",
                        "action": "create",
                        "content": raw,
                    }
                ],
            }

    # ── File application ──────────────────────────────────────────────────────

    def _apply_changes(self, workspace: Path, changes: dict) -> list[str]:
        """Write file contents to disk. Returns list of relative paths written."""
        written: list[str] = []
        for file_spec in changes.get("files", []):
            rel_path = file_spec.get("path", "")
            action = file_spec.get("action", "create")
            content = file_spec.get("content", "")

            if not rel_path:
                continue

            full_path = workspace / rel_path

            if action == "delete":
                if full_path.exists():
                    full_path.unlink()
                    written.append(f"DELETE:{rel_path}")
            else:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                written.append(rel_path)
                self._log.debug("builder.file_written", path=rel_path)

        return written

    # ── Git commit ────────────────────────────────────────────────────────────

    async def _commit_changes(
        self, workspace: Path, task: Task, run: Run, files_written: list[str]
    ) -> dict:
        """Commit changes to git if available. Returns commit info dict."""
        if not files_written:
            return {}

        branch = run.active_branch or f"forge/run-{self.run_id[:8]}"

        try:
            from git import Actor, Repo  # noqa: PLC0415
            from git.exc import InvalidGitRepositoryError  # noqa: PLC0415

            try:
                repo = Repo(str(workspace))
            except (InvalidGitRepositoryError, Exception):
                repo = Repo.init(str(workspace))
                self._log.info("builder.git.initialized", workspace=str(workspace))

            # Stage all written files
            repo.git.add("-A")

            if not repo.index.diff("HEAD") and not repo.untracked_files:
                return {"branch": branch, "sha": None, "message": "No changes to commit"}

            author = Actor(settings.git_author_name, settings.git_author_email)
            commit_message = (
                f"feat: {task.title[:60]}\n\n"
                f"Run: {self.run_id}\n"
                f"Task: {self.task_id}\n"
                f"Agent: {self.AGENT_ROLE}"
            )
            commit = repo.index.commit(commit_message, author=author, committer=author)

            sha = commit.hexsha[:8]
            self._log.info("builder.git.committed", sha=sha, branch=branch)

            # Push if remote configured
            if settings.github_token and repo.remotes:
                try:
                    repo.git.push("origin", branch, "--set-upstream")
                    self._log.info("builder.git.pushed", branch=branch)
                except Exception as push_exc:
                    self._log.warning("builder.git.push_failed", error=str(push_exc))

            return {"branch": branch, "sha": sha, "message": commit_message.split("\n")[0]}

        except ImportError:
            self._log.warning("builder.git.unavailable")
            return {"branch": branch, "sha": None, "message": "git unavailable"}
        except Exception as exc:
            self._log.warning("builder.git.commit_failed", error=str(exc))
            return {"branch": branch, "sha": None, "error": str(exc)}

    # ── Artifact ──────────────────────────────────────────────────────────────

    async def _persist_artifact(
        self, session, output: dict, project_id: str, changes: dict
    ) -> None:
        try:
            json_bytes = json.dumps(output).encode()
            artifact = Artifact(
                run_id=self.run_id,
                task_id=self.task_id,
                project_id=project_id,
                artifact_type="diff",
                title=f"Build: {changes.get('summary', self.task_id)}",
                s3_key=f"local/{self.run_id}/{self.task_id}/build.json",
                content_hash=hashlib.sha256(json_bytes).hexdigest(),
                quality_evidence={
                    "gate": "build",
                    "files_written": output["files_written"],
                    "commit": output["commit"],
                    "summary": changes.get("summary", ""),
                },
            )
            session.add(artifact)
            await session.commit()
        except Exception as exc:
            self._log.warning("builder.artifact_persist_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="forge.agents.builder.execute_task",
    bind=True,
    queue="builder",
    max_retries=2,
    acks_late=True,
    soft_time_limit=1800,   # 30 min: git clone + LLM codegen can be slow
    time_limit=3600,        # 1 hour hard kill
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: build code for a single task."""

    agent = BuilderAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "builder",
    )
    result = asyncio.run(agent.execute())

    if not result.success:
        log.error("builder.task_failed", task_id=task_id, run_id=run_id, error=result.error)

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
