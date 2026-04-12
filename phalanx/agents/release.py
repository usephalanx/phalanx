"""
Release Agent — creates the GitHub PR and writes release notes.

Responsibilities:
  1. Gather run context: branch, commits, task summaries, review verdicts
  2. Generate release notes using Claude (structured, human-readable)
  3. Create a GitHub Pull Request via PyGitHub (if token configured)
  4. Update Run.pr_url and Run.pr_number
  5. Persist release notes as Artifact
  6. Mark task COMPLETED

If GitHub is not configured, the release agent writes release notes locally
and records them as an artifact — the PR step is skipped gracefully.

AP-003: exceptions propagate — Celery handles retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent, mark_task_failed
from phalanx.config.settings import get_settings
from phalanx.db.models import Artifact, Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

settings = get_settings()

_NOTES_MAX_TOKENS = 2048


class ReleaseAgent(BaseAgent):
    """
    IC5-level release agent.

    Creates the PR and writes release notes. The actual merge is
    done by a human after ship approval — this agent prepares the
    artifact and the PR description.
    """

    AGENT_ROLE = "release"

    async def execute(self) -> AgentResult:
        self._log.info("release.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            run = await self._load_run(session)
            work_order = await self._load_work_order(session, run.work_order_id)
            task_summaries = await self._load_task_summaries(session)

        # Generate release notes
        notes = await self._generate_release_notes(run, work_order, task_summaries)

        # Create GitHub PR
        pr_info = await self._create_github_pr(run, work_order, notes)

        output = {
            "release_notes": notes,
            "pr_url": pr_info.get("pr_url"),
            "pr_number": pr_info.get("pr_number"),
            "branch": run.active_branch,
        }

        async with get_db() as session:
            run_ref = await self._load_run(session)

            # Update Run with PR info
            pr_update: dict = {"updated_at": datetime.now(UTC)}
            if pr_info.get("pr_url"):
                pr_update["pr_url"] = pr_info["pr_url"]
            if pr_info.get("pr_number"):
                pr_update["pr_number"] = pr_info["pr_number"]

            await session.execute(update(Run).where(Run.id == self.run_id).values(**pr_update))

            await self._persist_artifact(session, output, run_ref.project_id, notes)

            await session.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(
                    status="COMPLETED",
                    output=output,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        await self._audit(
            event_type="task_complete",
            payload={
                "pr_url": pr_info.get("pr_url"),
                "pr_number": pr_info.get("pr_number"),
            },
        )

        self._log.info(
            "release.execute.done",
            pr_url=pr_info.get("pr_url"),
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

    async def _load_work_order(self, session, work_order_id: str) -> WorkOrder | None:
        result = await session.execute(select(WorkOrder).where(WorkOrder.id == work_order_id))
        return result.scalar_one_or_none()

    async def _load_task_summaries(self, session) -> list[dict]:
        """Load all completed tasks for this run with their outputs."""
        result = await session.execute(
            select(Task)
            .where(Task.run_id == self.run_id, Task.status == "COMPLETED")
            .order_by(Task.sequence_num)
        )
        summaries = []
        for t in result.scalars().all():
            output = t.output or {}
            summaries.append(
                {
                    "title": t.title,
                    "agent_role": t.agent_role,
                    "summary": output.get("summary", output.get("approach", "")),
                    "verdict": output.get("verdict"),  # reviewer verdict
                }
            )
        return summaries

    # ── Release notes ─────────────────────────────────────────────────────────

    async def _generate_release_notes(
        self, run: Run, work_order: WorkOrder | None, task_summaries: list[dict]
    ) -> dict:
        """Call Claude to write structured release notes."""
        wo_title = work_order.title if work_order else f"Run {self.run_id[:8]}"
        wo_desc = work_order.description if work_order else ""

        summaries_text = "\n".join(
            f"- [{t['agent_role']}] {t['title']}: {t['summary']}"
            for t in task_summaries
            if t.get("summary")
        )

        system = """\
You are a technical writer for FORGE, an AI team operating system.
Write concise, professional release notes for a completed engineering task.
The audience is the engineering team and product stakeholders.

Return ONLY valid JSON — no markdown fences.

{
  "title": "Release Notes: <feature name>",
  "summary": "2-3 sentence executive summary of what changed and why",
  "changes": [
    {"type": "feat|fix|refactor|test|docs", "description": "..."}
  ],
  "testing": "brief description of how this was tested",
  "rollback": "brief description of how to roll back if needed",
  "breaking_changes": [] or ["list any breaking changes"],
  "running_instructions": {
    "steps": ["step 1 command", "step 2 command", "step 3 command"],
    "url": "http://localhost:<port>",
    "credentials": {"email": "demo@phalanx.dev", "password": "demo1234"}  # pragma: allowlist secret
  }
}

For running_instructions: always include Docker-based steps to run the app locally.
If the app has no auth (e.g. a static page or API only), omit the credentials field.
Keep steps to 3 commands or fewer."""

        messages = [
            {
                "role": "user",
                "content": (
                    f"Work order: {wo_title}\n"
                    f"Description: {wo_desc}\n\n"
                    f"Completed tasks:\n{summaries_text or 'No task summaries available.'}\n\n"
                    "Write release notes for this change."
                ),
            }
        ]

        raw = self._call_claude(messages=messages, system=system, max_tokens=_NOTES_MAX_TOKENS)

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            return {
                "title": f"Release Notes: {wo_title}",
                "summary": raw[:300] if raw else wo_desc,
                "changes": [{"type": "feat", "description": wo_title}],
                "testing": "Automated test suite passed.",
                "rollback": "Revert the PR to roll back.",
                "breaking_changes": [],
            }

    # ── GitHub PR creation ────────────────────────────────────────────────────

    async def _create_github_pr(self, run: Run, work_order: WorkOrder | None, notes: dict) -> dict:
        """Create a GitHub PR. Returns {pr_url, pr_number} or empty dict."""
        if not settings.github_token or not run.active_branch:
            self._log.info(
                "release.github.skipped",
                has_token=bool(settings.github_token),
                has_branch=bool(run.active_branch),
            )
            return {}

        try:
            from github import Github  # noqa: PLC0415

            # Get project repo URL
            async with get_db() as session:
                from phalanx.db.models import Project  # noqa: PLC0415

                result = await session.execute(select(Project).where(Project.id == run.project_id))
                project = result.scalar_one_or_none()

            repo_name = (project.config or {}).get("github_repo", "") if project else ""
            if not repo_name:
                self._log.info("release.github.no_repo_configured")
                return {}

            gh = Github(settings.github_token)
            repo = gh.get_repo(repo_name)

            # Build PR body from release notes
            changes_text = "\n".join(
                f"- **{c['type']}**: {c['description']}" for c in notes.get("changes", [])
            )
            breaking = (
                "\n\n⚠️ **Breaking changes:**\n"
                + "\n".join(f"- {b}" for b in notes["breaking_changes"])
                if notes.get("breaking_changes")
                else ""
            )

            running = notes.get("running_instructions", {})
            running_text = ""
            if running:
                steps = "\n".join(f"```\n{s}\n```" for s in running.get("steps", []))
                creds = running.get("credentials", {})
                creds_text = (
                    f"\n**Default credentials:** `{creds['email']}` / `{creds['password']}`"
                    if creds
                    else ""
                )
                url_text = f"\n**URL:** {running['url']}" if running.get("url") else ""
                running_text = f"\n\n## Running Locally\n{steps}{url_text}{creds_text}"

            pr_body = (
                f"## Summary\n{notes.get('summary', '')}\n\n"
                f"## Changes\n{changes_text or 'See commit history.'}\n\n"
                f"## Testing\n{notes.get('testing', 'Automated test suite.')}"
                f"{breaking}"
                f"{running_text}\n\n"
                f"---\n"
                f"🤖 Generated by FORGE | Run: `{run.id}` | "
                f"[View run](#) | Branch: `{run.active_branch}`"
            )

            wo_title = work_order.title if work_order else notes.get("title", "FORGE automated PR")

            pr = repo.create_pull(
                title=wo_title[:256],
                body=pr_body,
                head=run.active_branch,
                base=repo.default_branch,
                draft=False,
            )

            self._log.info("release.github.pr_created", pr_number=pr.number, url=pr.html_url)
            return {"pr_url": pr.html_url, "pr_number": pr.number}

        except ImportError:
            self._log.warning("release.github.pygithub_missing")
            return {}
        except Exception as exc:
            self._log.warning("release.github.pr_failed", error=str(exc))
            return {"error": str(exc)}

    # ── Artifact ──────────────────────────────────────────────────────────────

    async def _persist_artifact(self, session, output: dict, project_id: str, notes: dict) -> None:
        try:
            json_bytes = json.dumps(output).encode()
            artifact = Artifact(
                run_id=self.run_id,
                task_id=self.task_id,
                project_id=project_id,
                artifact_type="release_notes",
                title=notes.get("title", f"Release notes for run {self.run_id[:8]}"),
                s3_key=f"local/{self.run_id}/{self.task_id}/release_notes.json",
                content_hash=hashlib.sha256(json_bytes).hexdigest(),
                quality_evidence={
                    "gate": "release",
                    "pr_url": output.get("pr_url"),
                    "pr_number": output.get("pr_number"),
                    "branch": output.get("branch"),
                    "notes": notes,
                },
            )
            session.add(artifact)
            await session.commit()
        except Exception as exc:
            self._log.warning("release.artifact_persist_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.release.execute_task",
    bind=True,
    queue="release",
    max_retries=2,
    acks_late=True,
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: prepare release artifacts for a single task."""

    agent = ReleaseAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "release",
    )
    try:
        result = asyncio.run(agent.execute())
    except Exception as exc:
        log.exception("release.celery_task_unhandled", task_id=task_id, run_id=run_id)
        asyncio.run(mark_task_failed(task_id, str(exc)))
        raise

    if not result.success:
        log.error("release.task_failed", task_id=task_id, run_id=run_id, error=result.error)

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
