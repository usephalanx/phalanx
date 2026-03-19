"""
Planner Agent — translates a high-level task description into a concrete
implementation plan that the Builder can execute without ambiguity.

Responsibilities:
  1. Load task description + project context from the DB
  2. Gather prior task outputs from this run (for continuity)
  3. Call Claude (Opus) to generate a structured implementation plan
  4. Persist the plan as a 'plan' Artifact with quality_evidence
  5. Write plan to Task.output and mark COMPLETED

The plan is the Builder's source of truth: file paths, function names,
test cases — precise enough that no questions need to be asked.

Design notes:
  - Uses Claude Opus for reasoning quality (planning is the bottleneck).
  - Never modifies code; that is exclusively the Builder's domain.
  - AP-003: exceptions propagate to Celery retry handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update

from forge.agents.base import AgentResult, BaseAgent
from forge.db.models import Artifact, Run, Task
from forge.db.session import get_db
from forge.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

# Maximum token budget for plan generation.
# Opus needs room to think through complex architectures.
_PLAN_MAX_TOKENS = 6000


class PlannerAgent(BaseAgent):
    """
    IC5-level planning agent.

    Produces a structured, actionable implementation plan from a task
    description. Called by WorkflowOrchestrator before every Builder task.
    """

    AGENT_ROLE = "planner"

    async def execute(self) -> AgentResult:
        self._log.info("planner.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(
                    success=False,
                    output={},
                    error=f"Task {self.task_id} not found",
                )

            run = await self._load_run(session)
            prior_outputs = await self._load_prior_outputs(session, task.sequence_num)

        # Generate plan (outside DB session — Claude call can be slow)
        plan = await self._generate_plan(task, run, prior_outputs)

        async with get_db() as session:
            run = await self._load_run(session)  # fresh ref
            await self._persist_artifact(session, plan, run.project_id)
            await session.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(
                    status="COMPLETED",
                    output=plan,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        await self._audit(
            event_type="task_complete",
            payload={
                "steps": len(plan.get("implementation_steps", [])),
                "files": len(plan.get("files", [])),
            },
        )

        self._log.info("planner.execute.done", tokens_used=self._tokens_used)
        return AgentResult(success=True, output=plan, tokens_used=self._tokens_used)

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_run(self, session) -> Run:
        result = await session.execute(select(Run).where(Run.id == self.run_id))
        return result.scalar_one()

    async def _load_prior_outputs(self, session, before_seq: int) -> list[dict]:
        """Completed tasks before this one in the same run — for context."""
        result = await session.execute(
            select(Task)
            .where(
                Task.run_id == self.run_id,
                Task.sequence_num < before_seq,
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num)
        )
        return [
            {
                "sequence_num": t.sequence_num,
                "title": t.title,
                "agent_role": t.agent_role,
                "output": t.output or {},
            }
            for t in result.scalars().all()
        ]

    # ── Core logic ────────────────────────────────────────────────────────────

    async def _generate_plan(self, task: Task, run: Run, prior_outputs: list[dict]) -> dict:
        """Call Claude Opus to produce a structured implementation plan."""
        files_hint = (
            f"\nFiles likely touched: {', '.join(task.files_likely_touched)}"
            if task.files_likely_touched
            else ""
        )
        prior_ctx = ""
        if prior_outputs:
            prior_ctx = (
                "\n\nContext from prior tasks in this run:\n"
                + json.dumps(prior_outputs, indent=2)[:3000]
            )

        system = """\
You are a senior software architect in FORGE, an AI team operating system.
Your role: take a task description and produce a complete, unambiguous implementation
plan that a code-writing agent can execute without needing to ask any questions.

Rules:
- Be specific: name exact file paths, function/class names, method signatures.
- Every file change must have a clear "purpose" and list of "key_changes".
- Include a concrete test strategy with specific test function names.
- Acceptance criteria must be objectively verifiable (no "should work correctly").
- Complexity: 1 (trivial config change) to 10 (major architectural refactor).

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON object.

{
  "task_title": "...",
  "approach": "concise strategy description (1-2 sentences)",
  "files": [
    {
      "path": "relative/path/to/file.py",
      "action": "create|modify|delete",
      "purpose": "why this change is needed",
      "key_changes": ["specific change 1", "specific change 2"]
    }
  ],
  "implementation_steps": [
    "Step 1: ...",
    "Step 2: ..."
  ],
  "test_strategy": "specific tests to write with function names and what they verify",
  "acceptance_criteria": [
    "Criterion 1 (objectively verifiable)",
    "Criterion 2"
  ],
  "edge_cases": ["edge case 1", "edge case 2"],
  "estimated_complexity": 3
}"""

        messages = [
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\n\n"
                    f"Description: {task.description}"
                    f"{files_hint}"
                    f"{prior_ctx}\n\n"
                    "Produce a complete, implementation-ready plan. Be specific about "
                    "file paths, function names, and test cases."
                ),
            }
        ]

        raw = self._call_claude(messages=messages, system=system, max_tokens=_PLAN_MAX_TOKENS)

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            plan = json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            self._log.warning("planner.json_parse_failed", raw_len=len(raw))
            plan = {
                "task_title": task.title,
                "approach": raw[:500] if raw else task.description,
                "files": [],
                "implementation_steps": [task.description],
                "test_strategy": "Write unit tests for all new functions.",
                "acceptance_criteria": ["All tests pass.", "No lint errors."],
                "edge_cases": [],
                "estimated_complexity": task.estimated_complexity,
            }

        return plan

    async def _persist_artifact(self, session, plan: dict, project_id: str) -> None:
        try:
            json_bytes = json.dumps(plan).encode()
            artifact = Artifact(
                run_id=self.run_id,
                task_id=self.task_id,
                project_id=project_id,
                artifact_type="plan",
                title=f"Plan: {plan.get('task_title', self.task_id)}",
                s3_key=f"local/{self.run_id}/{self.task_id}/plan.json",
                content_hash=hashlib.sha256(json_bytes).hexdigest(),
                quality_evidence={
                    "gate": "planning",
                    "steps": len(plan.get("implementation_steps", [])),
                    "files_touched": len(plan.get("files", [])),
                    "criteria": plan.get("acceptance_criteria", []),
                    "complexity": plan.get("estimated_complexity", 3),
                    "plan": plan,
                },
            )
            session.add(artifact)
            await session.commit()
        except Exception as exc:
            self._log.warning("planner.artifact_persist_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="forge.agents.planner.execute_task",
    bind=True,
    queue="planner",
    max_retries=2,
    acks_late=True,
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: plan a single task. Called by WorkflowOrchestrator."""

    agent = PlannerAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "planner",
    )
    result = asyncio.get_event_loop().run_until_complete(agent.execute())

    if not result.success:
        log.error("planner.task_failed", task_id=task_id, run_id=run_id, error=result.error)

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
