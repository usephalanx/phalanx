"""
Commander Agent — the single coordinator for a Run.

Responsibilities:
  1. Accept a WorkOrder, create a Run row (INTAKE)
  2. Research phase: load project memory, understand the work
  3. Planning phase: decompose into ordered Tasks, write to Postgres
  4. Request human plan approval (AWAITING_PLAN_APPROVAL)
  5. On approval: transition to EXECUTING, delegate to WorkflowOrchestrator
  6. On completion: request ship approval, then READY_TO_MERGE

Design (evidence in EXECUTION_PLAN.md §B):
  AD-001: Commander uses Anthropic API for planning/reasoning.
          Builder tasks (dispatched later) use Claude Code SDK subprocess.
  AP-001: One Commander per Run — no parallel commanders.
  AP-004: Every state change goes through the state machine.
  AP-003: Exceptions propagate up to Celery retry handler — never swallowed.

Celery task entry point: forge.agents.commander.execute_run
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.config.loader import ConfigLoader
from phalanx.db.models import Run, Task, TaskDependency, WorkOrder
from phalanx.memory.assembler import MemoryAssembler
from phalanx.memory.reader import MemoryReader
from phalanx.queue.celery_app import celery_app
from phalanx.runtime.task_router import TaskRouter
from phalanx.workflow.approval_gate import (
    ApprovalGate,
    ApprovalRejectedError,
    ApprovalTimeoutError,
)
from phalanx.workflow.orchestrator import WorkflowOrchestrator
from phalanx.workflow.slack_notifier import SlackNotifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


class CommanderAgent(BaseAgent):
    """
    IC6-level orchestrator. Creates and drives a single Run to completion.

    Instantiated from the `execute_run` Celery task — one instance per Run.
    """

    AGENT_ROLE = "commander"

    def __init__(
        self,
        run_id: str,
        work_order_id: str,
        project_id: str,
        agent_id: str = "commander",
    ) -> None:
        super().__init__(run_id=run_id, agent_id=agent_id)
        self.work_order_id = work_order_id
        self.project_id = project_id
        self._loader = ConfigLoader()

    async def execute(self) -> AgentResult:
        """Drive the full Run lifecycle."""
        from phalanx.db.session import get_db  # noqa: PLC0415

        self._log.info("commander.execute.start")

        async with get_db() as session:
            # Load work order
            wo = await self._load_work_order(session)
            if wo is None:
                return AgentResult(
                    success=False,
                    output={},
                    error=f"WorkOrder {self.work_order_id} not found",
                )

            # ── Phase 1: INTAKE → RESEARCHING ─────────────────────────────────
            await self._create_or_load_run(session, wo)
            await self._transition_run("INTAKE", "RESEARCHING")
            await self._audit("state_transition", from_state="INTAKE", to_state="RESEARCHING")

            # Build Slack notifier once — all subsequent posts go to the thread
            # anchored on WorkOrder.slack_thread_ts (set by gateway).
            # Returns a silent no-op notifier if flag is off, token is missing,
            # or the run has no registered Slack channel (simulator / API path).
            notifier = await SlackNotifier.from_run(self.run_id, session)
            await notifier.post(f"🧠 Planning your *{wo.title}*…")

            # ── Phase 2: Load memory context ──────────────────────────────────
            reader = MemoryReader(session, self.project_id)
            standing_facts = await reader.get_standing_facts()
            decisions = await reader.get_standing_decisions()
            assembler = MemoryAssembler(max_tokens=4000)
            memory_block = assembler.build(decisions=decisions, standing_facts=standing_facts)

            # ── Phase 3: RESEARCHING → PLANNING ───────────────────────────────
            await self._transition_run("RESEARCHING", "PLANNING")

            task_plan = await self._generate_task_plan(wo, memory_block)

            # Write tasks to Postgres
            await self._persist_task_plan(session, task_plan)

            # ── Phase 4: PLANNING → AWAITING_PLAN_APPROVAL ────────────────────
            await self._transition_run("PLANNING", "AWAITING_PLAN_APPROVAL")

            gate = ApprovalGate(
                session=session,
                run_id=self.run_id,
                timeout_seconds=self._loader.workflow.workflow.approval_timeout_hours * 3600,
            )

            try:
                await gate.request_and_wait(
                    gate_type="plan",
                    gate_phase="planning",
                    context_snapshot={"plan": task_plan},
                )
            except ApprovalRejectedError as exc:
                self._log.warning("commander.plan_rejected", reason=str(exc))
                await self._transition_run(
                    "AWAITING_PLAN_APPROVAL", "PLANNING", error_message=f"Plan rejected: {exc}"
                )
                return AgentResult(
                    success=False,
                    output={},
                    error=str(exc),
                    tokens_used=self._tokens_used,
                )
            except ApprovalTimeoutError as exc:
                await self._transition_run(
                    "AWAITING_PLAN_APPROVAL",
                    "FAILED",
                    error_message=str(exc),
                )
                return AgentResult(
                    success=False,
                    output={},
                    error=str(exc),
                    tokens_used=self._tokens_used,
                )

            # ── Plan approved: post summary to Slack thread ───────────────────
            # Load the tasks we persisted so run_planned can show role breakdown.
            # This is a cheap indexed read (Task.run_id is indexed).
            approved_tasks_result = await session.execute(
                select(Task).where(Task.run_id == self.run_id).order_by(Task.sequence_num)
            )
            await notifier.run_planned(list(approved_tasks_result.scalars()))

            # ── Phase 5: AWAITING_PLAN_APPROVAL → EXECUTING ───────────────────
            await self._transition_run("AWAITING_PLAN_APPROVAL", "EXECUTING")

            router = TaskRouter(celery_app)
            orch = WorkflowOrchestrator(
                session=session,
                run_id=self.run_id,
                task_router=router,
                approval_timeout_hours=self._loader.workflow.workflow.approval_timeout_hours,
                notifier=notifier,
            )

            try:
                await orch.execute()  # drives all tasks → VERIFYING
            except Exception as exc:
                self._log.exception("commander.execution_failed", error=str(exc))
                await self._transition_run(
                    "EXECUTING",
                    "FAILED",
                    error_message=str(exc),
                    error_context={"phase": "execution"},
                )
                return AgentResult(
                    success=False,
                    output={},
                    error=str(exc),
                    tokens_used=self._tokens_used,
                )

            # ── Phase 6: VERIFYING → AWAITING_SHIP_APPROVAL ───────────────────
            await self._transition_run("VERIFYING", "AWAITING_SHIP_APPROVAL")

            try:
                await orch.request_ship_approval(
                    context_snapshot={"task_count": len(task_plan.get("tasks", []))}
                )
            except ApprovalRejectedError as exc:
                await self._transition_run(
                    "AWAITING_SHIP_APPROVAL",
                    "EXECUTING",
                    error_message=f"Ship rejected — rework required: {exc}",
                )
                return AgentResult(
                    success=False,
                    output={},
                    error=str(exc),
                    tokens_used=self._tokens_used,
                )

        self._log.info("commander.execute.complete", tokens_used=self._tokens_used)
        return AgentResult(
            success=True,
            output={"run_id": self.run_id, "status": "READY_TO_MERGE"},
            tokens_used=self._tokens_used,
        )

    async def _load_work_order(self, session: AsyncSession) -> WorkOrder | None:
        result = await session.execute(select(WorkOrder).where(WorkOrder.id == self.work_order_id))
        return result.scalar_one_or_none()

    async def _create_or_load_run(self, session: AsyncSession, wo: WorkOrder) -> Run:
        """Create a new Run for this work order (run_number auto-incremented)."""
        from sqlalchemy import func  # noqa: PLC0415

        # Count existing runs for this work order to get next run_number
        count_result = await session.execute(
            select(func.count()).select_from(Run).where(Run.work_order_id == wo.id)
        )
        existing_count = count_result.scalar_one()

        run = Run(
            id=self.run_id,
            work_order_id=wo.id,
            project_id=wo.project_id,
            run_number=existing_count + 1,
            status="INTAKE",
        )
        session.add(run)
        await session.commit()
        return run

    async def _generate_task_plan(self, wo: WorkOrder, memory_block: str) -> dict:
        """
        Build the task plan for the current phase.

        If WorkOrder has enriched_spec (from PromptEnricher), use the current phase's
        claude_prompt directly — no Claude call needed for decomposition.

        Falls back to Claude decomposition if enrichment is absent or disabled.
        """
        # ── Enriched path: use phase spec from PromptEnricher ──────────────────
        current_phase_num = getattr(wo, "current_phase", None)
        current_phase_num = current_phase_num if isinstance(current_phase_num, int) else 0
        enriched_spec = getattr(wo, "enriched_spec", None)
        if enriched_spec and isinstance(enriched_spec, dict) and current_phase_num >= 1:
            phases = enriched_spec.get("phases", [])
            phase_idx = current_phase_num - 1  # 1-indexed → 0-indexed
            if 0 <= phase_idx < len(phases):
                phase = phases[phase_idx]
                self._log.info(
                    "commander.plan_from_enriched_spec",
                    phase_id=phase.get("id"),
                    phase_name=phase.get("name"),
                    total_phases=len(phases),
                )
                return self._build_plan_from_phase(phase, wo)

        # ── Fallback: Claude decomposition (no enrichment or phase exhausted) ──
        return await self._plan_via_claude(wo, memory_block)

    def _build_plan_from_phase(self, phase: dict, wo: WorkOrder) -> dict:
        """
        Build a single-task plan from a PhaseSpec.

        One builder task per phase — the claude_prompt IS the task description.
        role_context and phase metadata are attached for Builder to use.
        """
        role = phase.get("role", {})
        role_context = (
            f"[ROLE]\n"
            f"Title: {role.get('title', 'Senior Software Engineer')}\n"
            f"Seniority: {role.get('seniority', 'Senior')}\n"
            f"Domain: {role.get('domain', '')}\n\n"
            f"{role.get('persona', '')}"
        ).strip()

        claude_prompt = phase.get("claude_prompt", "")
        if not claude_prompt:
            # Fall back to assembling from structured fields
            objectives = "\n".join(f"- {o}" for o in phase.get("objectives", []))
            deliverables = "\n".join(
                f"- {d.get('file', '')}: {d.get('description', '')}"
                for d in phase.get("deliverables", [])
            )
            claude_prompt = (
                f"{phase.get('context', '')}\n\n"
                f"Objectives:\n{objectives}\n\n"
                f"Deliverables:\n{deliverables}"
            )

        return {
            "tasks": [
                {
                    "sequence_num": 1,
                    "title": f"[Phase {phase.get('id', '?')}] {phase.get('name', wo.title)}",
                    "description": claude_prompt,
                    "agent_role": phase.get("agent_role", "builder"),
                    "depends_on": [],
                    "files_likely_touched": [
                        d.get("file", "") for d in phase.get("deliverables", []) if d.get("file")
                    ],
                    "estimated_complexity": 4,
                    # Enricher metadata — consumed by BuilderAgent
                    "_phase_id": phase.get("id"),
                    "_phase_name": phase.get("name"),
                    "_role_context": role_context,
                }
            ]
        }

    async def _plan_via_claude(self, wo: WorkOrder, memory_block: str) -> dict:
        """
        Original Claude-based task decomposition (fallback path).
        Used when enrichment is disabled or not available.
        """
        system = f"""You are the Commander in FORGE, an AI team operating system.
Your job is to decompose a work order into an ordered list of tasks for different agents.

{memory_block}

Agents available: planner, builder, reviewer, qa, security, release
Rules:
- Each task has exactly ONE agent_role owner
- Tasks must be ordered by sequence_num (dependency order)
- builder tasks always precede reviewer/qa tasks
- Include a test task (qa agent_role) after every implementation task
- Output ONLY valid JSON — no markdown, no explanation

JSON format:
{{
  "tasks": [
    {{
      "sequence_num": 1,
      "title": "...",
      "description": "...",
      "agent_role": "planner|builder|reviewer|qa|security|release",
      "depends_on": [],
      "files_likely_touched": [],
      "estimated_complexity": 3
    }}
  ]
}}"""

        messages = [
            {
                "role": "user",
                "content": (
                    f"Work order: {wo.title}\n\n"
                    f"Description: {wo.description}\n\n"
                    f"Decompose this into tasks. Be specific about what each agent must do."
                ),
            }
        ]

        response_text = self._call_claude(
            messages=messages,
            system=system,
            max_tokens=8192,  # Increased from 4096 to prevent JSON truncation
        )

        try:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            return json.loads(response_text[start:end])
        except (json.JSONDecodeError, ValueError) as exc:
            self._log.error("commander.plan_parse_failed", error=str(exc))
            return {
                "tasks": [
                    {
                        "sequence_num": 1,
                        "title": wo.title,
                        "description": wo.description,
                        "agent_role": "builder",
                        "depends_on": [],
                        "files_likely_touched": [],
                        "estimated_complexity": 3,
                    }
                ]
            }

    async def _persist_task_plan(self, session: AsyncSession, plan: dict) -> None:
        """Write all tasks and their DAG dependency edges to Postgres."""
        tasks_data = plan.get("tasks", [])

        # Detect which optional columns exist in the deployed model
        _task_cols = {c.key for c in Task.__table__.columns}

        # First pass: create Task rows, map sequence_num → Task object
        seq_to_task: dict[int, Task] = {}
        for t in tasks_data:
            task = Task(
                run_id=self.run_id,
                sequence_num=t.get("sequence_num", 1),
                title=t.get("title", "Untitled task"),
                description=t.get("description", ""),
                agent_role=t.get("agent_role", "builder"),
                status="PENDING",
                depends_on=[str(d) for d in t.get("depends_on", [])],
                files_likely_touched=t.get("files_likely_touched", []),
                estimated_complexity=t.get("estimated_complexity", 3),
            )
            # Phase enricher metadata — only set if columns exist in deployed model
            if "phase_id" in _task_cols:
                task.phase_id = t.get("_phase_id")
            if "phase_name" in _task_cols:
                task.phase_name = t.get("_phase_name")
            if "role_context" in _task_cols:
                task.role_context = t.get("_role_context")
            session.add(task)
            seq_to_task[t.get("sequence_num", 1)] = task

        # Flush to get DB-assigned UUIDs without committing yet
        await session.flush()

        # Second pass: write TaskDependency edges so DagResolver can build the graph
        dep_count = 0
        for t in tasks_data:
            child_seq = t.get("sequence_num", 1)
            child_task = seq_to_task[child_seq]
            for dep_seq in t.get("depends_on", []):
                parent_task = seq_to_task.get(int(dep_seq))
                if parent_task:
                    session.add(TaskDependency(
                        task_id=child_task.id,
                        depends_on_id=parent_task.id,
                        dependency_type="full",
                    ))
                    dep_count += 1

        await session.commit()
        self._log.info(
            "commander.plan_persisted",
            task_count=len(tasks_data),
            dependency_edges=dep_count,
        )


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.commander.execute_run",
    bind=True,
    queue="commander",
    max_retries=2,
    acks_late=True,
    soft_time_limit=3600,   # 1 hour: full pipeline (plan+build+review+qa+security+release) needs time
    time_limit=7200,        # 2 hour hard kill
)
def execute_run(
    self, work_order_id: str, project_id: str, run_id: str, **kwargs
):  # pragma: no cover
    """
    Celery task: bootstrap and drive a Commander for a single Run.

    This is the ONLY entry point for starting a new run.
    The work_order_id, project_id, and run_id are passed by the Slack gateway.
    """
    import asyncio  # noqa: PLC0415

    agent = CommanderAgent(
        run_id=run_id,
        work_order_id=work_order_id,
        project_id=project_id,
    )

    result = asyncio.run(agent.execute())

    if not result.success:
        # Celery will retry if we raise — but commander failure is usually
        # human-correctable (plan rejected, etc.), not a transient error.
        # Log and return; state machine already wrote FAILED.
        log.error(
            "commander.celery_task_failed",
            run_id=run_id,
            error=result.error,
        )
    return {
        "success": result.success,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
    }
