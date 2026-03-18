"""
Reviewer Agent — evaluates code changes against quality standards.

Responsibilities:
  1. Load task + builder's output (files written, commit info)
  2. Read current file contents from the workspace
  3. Call Claude to review against coding standards, architecture, correctness
  4. Produce a structured review: verdict (APPROVED | CHANGES_REQUESTED | CRITICAL_ISSUES)
  5. Persist review as Artifact (always — even for approvals)
  6. Mark task COMPLETED with verdict in output

Review philosophy:
  - APPROVED: code is shippable with minor suggestions (if any)
  - CHANGES_REQUESTED: notable issues that should be addressed; non-blocking for MVP
  - CRITICAL_ISSUES: security holes, data loss bugs, broken contracts — escalates

For MVP the reviewer always sets Task.status = COMPLETED. The verdict is
visible at the ship approval gate where a human makes the final call.
Only CRITICAL_ISSUES escalates to ESCALATING status (which pauses the run).

AP-003: exceptions propagate — Celery handles retries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select, update

from forge.agents.base import AgentResult, BaseAgent
from forge.config.settings import get_settings
from forge.db.models import Artifact, Run, Task
from forge.db.session import get_db
from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

settings = get_settings()

_REVIEW_MAX_TOKENS = 4096
# Max bytes of code to send for review
_MAX_CODE_BYTES = 30_000


class ReviewerAgent(BaseAgent):
    """
    IC5-level code review agent.

    Reviews the builder's output against FORGE quality standards.
    Verdict is persisted as an Artifact and included in Task.output
    for visibility at the ship approval gate.
    """

    AGENT_ROLE = "reviewer"

    async def execute(self) -> AgentResult:
        self._log.info("reviewer.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(
                    success=False, output={}, error=f"Task {self.task_id} not found"
                )
            run = await self._load_run(session)
            builder_output = await self._load_builder_output(session, task.sequence_num)

        # Read changed files from workspace
        workspace = Path(settings.git_workspace) / run.project_id / self.run_id
        code_context = self._read_changed_files(workspace, builder_output)

        # Run the review
        review = await self._run_review(task, builder_output, code_context)

        async with get_db() as session:
            run_ref = await self._load_run(session)
            await self._persist_artifact(session, review, run_ref.project_id)

            # Determine task status based on verdict
            task_status = "COMPLETED"
            escalation_reason = None
            if review.get("verdict") == "CRITICAL_ISSUES":
                task_status = "ESCALATING"
                escalation_reason = review.get("blocking_reason", "Critical issues found in review")
                self._log.warning(
                    "reviewer.critical_issues_found",
                    reason=escalation_reason,
                )

            update_values: dict = {
                "status": task_status,
                "output": review,
                "completed_at": datetime.now(UTC),
            }
            if escalation_reason:
                update_values["escalation_reason"] = escalation_reason

            await session.execute(
                update(Task).where(Task.id == self.task_id).values(**update_values)
            )
            await session.commit()

        await self._audit(
            event_type="task_complete",
            payload={
                "verdict": review.get("verdict"),
                "issues_count": len(review.get("issues", [])),
            },
        )

        self._log.info(
            "reviewer.execute.done",
            verdict=review.get("verdict"),
            tokens_used=self._tokens_used,
        )
        return AgentResult(success=True, output=review, tokens_used=self._tokens_used)

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_run(self, session) -> Run:
        result = await session.execute(select(Run).where(Run.id == self.run_id))
        return result.scalar_one()

    async def _load_builder_output(self, session, before_seq: int) -> dict:
        """Find the most recent completed builder task output in this run."""
        result = await session.execute(
            select(Task)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "builder",
                Task.sequence_num < before_seq,
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        return task.output or {} if task else {}

    # ── File reading ──────────────────────────────────────────────────────────

    def _read_changed_files(self, workspace: Path, builder_output: dict) -> str:
        """Read files that were written by the builder."""
        if not workspace.exists():
            return ""

        files_written = builder_output.get("files_written", [])
        parts: list[str] = []
        total_bytes = 0

        for rel_path in files_written:
            if rel_path.startswith("DELETE:"):
                parts.append(f"--- {rel_path[7:]} (DELETED) ---")
                continue
            full = workspace / rel_path
            if not full.exists():
                continue
            try:
                content = full.read_text(errors="replace")
                truncated = content[:12_000]
                parts.append(f"--- {rel_path} ---\n{truncated}")
                total_bytes += len(truncated.encode())
                if total_bytes >= _MAX_CODE_BYTES:
                    parts.append("... (truncated for token limit)")
                    break
            except OSError:
                pass

        return "\n\n".join(parts)

    # ── Review logic ──────────────────────────────────────────────────────────

    async def _run_review(
        self, task: Task, builder_output: dict, code_context: str
    ) -> dict:
        """Call Claude to review the code changes."""

        summary = builder_output.get("summary", "No builder summary available.")
        commit = builder_output.get("commit", {})

        system = """\
You are a senior code reviewer in FORGE, an AI team operating system.
Your role: review code changes for correctness, security, maintainability, and style.

Review standards:
- Security: no hardcoded secrets, no SQL injection, no XSS vectors, safe subprocess usage
- Correctness: logic is sound, edge cases handled, no off-by-one errors
- Maintainability: clear names, docstrings on public APIs, no dead code
- Tests: new functionality must have tests; tests must be meaningful (no trivial asserts)
- Style: follows existing patterns, type annotations present, imports organized

Verdict definitions:
- APPROVED: code is ready to ship; suggestions are optional improvements
- CHANGES_REQUESTED: notable issues that reduce quality; non-blocking but should be fixed
- CRITICAL_ISSUES: security vulnerabilities, data loss risk, or broken system contracts

Return ONLY valid JSON — no markdown fences.

{
  "verdict": "APPROVED|CHANGES_REQUESTED|CRITICAL_ISSUES",
  "summary": "one paragraph review summary",
  "blocking_reason": null or "description of what blocks shipping",
  "issues": [
    {
      "severity": "critical|high|medium|low|suggestion",
      "location": "file.py:line_number or general",
      "description": "what the issue is",
      "suggestion": "how to fix it"
    }
  ],
  "positives": ["what was done well"],
  "test_coverage_ok": true|false,
  "security_ok": true|false
}"""

        code_section = (
            f"\n\nCode changes:\n{code_context}"
            if code_context
            else "\n\nNo code context available — reviewing based on task description."
        )

        messages = [
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\n"
                    f"Description: {task.description}\n"
                    f"Builder summary: {summary}\n"
                    f"Commit: {commit.get('message', 'N/A')}"
                    f"{code_section}\n\n"
                    "Review these changes. Be thorough but fair."
                ),
            }
        ]

        raw = self._call_claude(messages=messages, system=system, max_tokens=_REVIEW_MAX_TOKENS)

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            self._log.warning("reviewer.json_parse_failed")
            return {
                "verdict": "CHANGES_REQUESTED",
                "summary": raw[:500] if raw else "Review parsing failed.",
                "blocking_reason": None,
                "issues": [],
                "positives": [],
                "test_coverage_ok": None,
                "security_ok": None,
            }

    # ── Artifact ──────────────────────────────────────────────────────────────

    async def _persist_artifact(self, session, review: dict, project_id: str) -> None:
        try:
            json_bytes = json.dumps(review).encode()
            artifact = Artifact(
                run_id=self.run_id,
                task_id=self.task_id,
                project_id=project_id,
                artifact_type="code_review",
                title=f"Review: {review.get('verdict', 'unknown')}",
                s3_key=f"local/{self.run_id}/{self.task_id}/review.json",
                content_hash=hashlib.sha256(json_bytes).hexdigest(),
                quality_evidence={
                    "gate": "review",
                    "verdict": review.get("verdict"),
                    "issues_count": len(review.get("issues", [])),
                    "security_ok": review.get("security_ok"),
                    "test_coverage_ok": review.get("test_coverage_ok"),
                    "blocking_reason": review.get("blocking_reason"),
                    "review": review,
                },
            )
            session.add(artifact)
            await session.commit()
        except Exception as exc:
            self._log.warning("reviewer.artifact_persist_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="forge.agents.reviewer.execute_task",
    bind=True,
    queue="reviewer",
    max_retries=2,
    acks_late=True,
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: review code changes for a single task."""
    import asyncio  # noqa: PLC0415

    agent = ReviewerAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "reviewer",
    )
    result = asyncio.get_event_loop().run_until_complete(agent.execute())

    if not result.success:
        log.error("reviewer.task_failed", task_id=task_id, run_id=run_id, error=result.error)

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
