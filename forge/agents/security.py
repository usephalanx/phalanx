"""
Security Agent — runs mandatory pre-ship security scans.

Wraps the SecurityPipeline and presents its output as a Task artifact.
This agent runs as an independent task (not part of the builder/reviewer chain)
so it can execute in parallel with QA in future milestones.

Responsibilities:
  1. Load task + resolve workspace from Run.active_branch / project config
  2. Run SecurityPipeline: detect-secrets, bandit, pip-audit, (trivy optional)
  3. Persist SecurityScanResult as a 'security_report' Artifact
  4. Mark task COMPLETED — the scan result (pass/fail) is surfaced at the
     ship approval gate; it does NOT directly fail the Run here
     (security_override approval is the human bypass path)

Design (evidence in EXECUTION_PLAN.md §B):
  - AP-003: exceptions propagate — Celery handles retries.
  - The SecurityPipeline itself is non-bypassable in code (EXECUTION_PLAN AD-003).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select, update

from forge.agents.base import AgentResult, BaseAgent
from forge.config.settings import get_settings
from forge.db.models import Run, Task
from forge.db.session import get_db
from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

settings = get_settings()


class SecurityAgent(BaseAgent):
    """
    IC5-level security scanning agent.

    Runs the SecurityPipeline against the workspace and records results.
    Scan failures are advisory at the task level — the ship approval gate
    is where a human decides whether to proceed or request a security override.
    """

    AGENT_ROLE = "security"

    async def execute(self) -> AgentResult:
        self._log.info("security.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(
                    success=False, output={}, error=f"Task {self.task_id} not found"
                )
            run = await self._load_run(session)

        workspace = Path(settings.git_workspace) / run.project_id / self.run_id

        # Run security pipeline
        scan_result = await self._run_security_pipeline(workspace, run)

        output = {
            "workspace": str(workspace),
            "overall_passed": scan_result.get("overall_passed", False),
            "max_severity": scan_result.get("max_severity", "none"),
            "blocking_reason": scan_result.get("blocking_reason"),
            "scan_count": len(scan_result.get("scans", [])),
        }

        async with get_db() as session:
            # Always COMPLETED — the gate decision is at ship approval
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
                "passed": output["overall_passed"],
                "max_severity": output["max_severity"],
            },
        )

        self._log.info(
            "security.execute.done",
            passed=output["overall_passed"],
            severity=output["max_severity"],
        )
        return AgentResult(success=True, output=output, tokens_used=0)

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_run(self, session) -> Run:
        result = await session.execute(select(Run).where(Run.id == self.run_id))
        return result.scalar_one()

    # ── Pipeline execution ────────────────────────────────────────────────────

    async def _run_security_pipeline(self, workspace: Path, run: Run) -> dict:
        """
        Run SecurityPipeline if workspace exists; return a minimal result dict
        if workspace is not yet initialised (pipeline handles tool absence gracefully).
        """
        try:
            from forge.guardrails.security_pipeline import SecurityPipeline  # noqa: PLC0415

            pipeline = SecurityPipeline(
                run_id=run.id,
                repo_path=workspace,
                task_id=self.task_id,
                project_id=run.project_id,
            )
            result = await pipeline.run()

            # SecurityScanResult is a dataclass — convert to dict for storage
            return {
                "overall_passed": result.overall_passed,
                "max_severity": result.max_severity,
                "blocking_reason": result.blocking_reason,
                "scanned_at": result.scanned_at.isoformat(),
                "scans": [
                    {
                        "tool": s.tool,
                        "passed": s.passed,
                        "max_severity": s.max_severity,
                        "findings_count": len(s.findings),
                        "error": s.error,
                    }
                    for s in result.scans
                ],
            }

        except Exception as exc:
            self._log.warning("security.pipeline_failed", error=str(exc))
            # Non-fatal: if tools aren't installed, record degraded result
            return {
                "overall_passed": False,
                "max_severity": "unknown",
                "blocking_reason": f"Security pipeline error: {exc}",
                "scanned_at": datetime.now(UTC).isoformat(),
                "scans": [],
                "error": str(exc),
            }


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="forge.agents.security.execute_task",
    bind=True,
    queue="security",
    max_retries=2,
    acks_late=True,
)
def execute_task(
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: run security scans for a single task."""
    import asyncio  # noqa: PLC0415

    agent = SecurityAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "security",
    )
    result = asyncio.get_event_loop().run_until_complete(agent.execute())

    if not result.success:
        log.error("security.task_failed", task_id=task_id, run_id=run_id, error=result.error)

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
