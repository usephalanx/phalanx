"""
Verifier Agent — profile-driven post-DAG build smoke test.

Runs after IntegrationWiringAgent completes (which itself runs after all builders).
Uses VerificationProfile to determine the correct install/build/typecheck commands
for the detected tech stack — no hardcoded if/else, just profile lookup.

Supported tech stacks (see verification_profiles.py):
  web:    nextjs, vite, sveltekit, generic_web
  api:    fastapi, django, express, go, generic_python
  mobile: react_native, expo, flutter
  cli:    click_cli

On failure: marks task ESCALATING with structured error list. Non-fatal in the
orchestrator — pipeline continues to ship approval with the error noted.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.agents.verification_profiles import (
    detect_tech_stack,
    get_profile,
    merge_workspace,
    run_profile_checks,
)
from phalanx.config.settings import get_settings
from phalanx.db.models import Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Celery entry-point
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(
    name="phalanx.agents.verifier.execute_task",
    bind=True,
    max_retries=1,
    soft_time_limit=300,
    time_limit=360,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:
    agent = VerifierAgent(run_id=run_id, task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

class VerifierAgent(BaseAgent):
    AGENT_ROLE = "verifier"

    async def execute(self) -> AgentResult:
        self._log.info("verifier.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            run = await self._load_run(session)

            stmt = select(Task).where(
                Task.run_id == str(self.run_id),
                Task.agent_role == "builder",
            )
            result = await session.execute(stmt)
            builder_tasks = list(result.scalars().all())

        app_type = run.app_type or "web"
        base = Path(settings.git_workspace) / run.project_id / self.run_id

        # Use the merged workspace produced by IntegrationWiringAgent if it
        # exists, otherwise merge again from epic dirs.
        merged_dir = base / "_merged"
        if not merged_dir.exists() or not any(merged_dir.iterdir()):
            merged_dir = merge_workspace(base, builder_tasks)

        # Resolve tech_stack: planning hint → filesystem detection → fallback
        planning_hint = (task.output or {}).get("tech_stack", "") if task else ""
        tech_stack = planning_hint or detect_tech_stack(merged_dir, app_type)
        profile = get_profile(tech_stack)

        self._log.info(
            "verifier.profile_resolved",
            tech_stack=tech_stack,
            app_type=app_type,
            build_cmd=profile.build_cmd,
            merged_dir=str(merged_dir),
        )

        # Run all profile checks
        errors = run_profile_checks(profile, merged_dir)

        # Write result — merge with planning hint so tech_stack survives
        verdict = "APPROVED" if not errors else "CRITICAL_ISSUES"
        output = {
            **(task.output or {}),
            "verdict": verdict,
            "tech_stack": tech_stack,
            "errors": errors,
        }

        async with get_db() as session:
            await session.execute(
                update(Task)
                .where(Task.id == str(self.task_id))
                .values(
                    status="COMPLETED" if not errors else "ESCALATING",
                    output=output,
                    completed_at=datetime.now(UTC),
                    escalation_reason="; ".join(errors[:3]) if errors else None,
                )
            )
            await session.commit()

        if errors:
            self._log.warning(
                "verifier.build_failed",
                tech_stack=tech_stack,
                error_count=len(errors),
                first=errors[0],
            )
            return AgentResult(
                success=False,
                output=output,
                error=f"Build verification failed ({tech_stack}): {errors[0]}",
            )

        self._log.info("verifier.execute.done", verdict="APPROVED", tech_stack=tech_stack)
        return AgentResult(success=True, output=output)
