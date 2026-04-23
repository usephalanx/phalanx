"""CI Fixer v3 — Commander agent (orchestrator).

Phase 1 implementation. Zero changes to build flow or CI Fixer v2.

Lifecycle (per run):
  1. INTAKE → RESEARCHING → PLANNING          (ceremony, no LLM)
  2. Persist task DAG: cifix_techlead(seq=1), cifix_engineer(seq=2)
  3. PLANNING → AWAITING_PLAN_APPROVAL → EXECUTING   (skip ApprovalGate —
     CI fixes are auto-commit per ci_integrations.auto_commit, not human-gated)
  4. Fire advance_run_task. advance_run walks the DAG, dispatching each
     Task to its agent's Celery queue. When all Tasks COMPLETE, advance_run
     transitions EXECUTING → VERIFYING.
  5. Commander polls the Run for a terminal state:
       - VERIFYING → walk VERIFYING → AWAITING_SHIP_APPROVAL → READY_TO_MERGE
         → MERGED → RELEASE_PREP → AWAITING_RELEASE_APPROVAL → SHIPPED
         (state-machine chain; no approval gates invoked)
       - FAILED → exit with the run's error_message
       - timeout → force FAILED

Invariants:
  - Never reads code. Never calls sandbox. Never calls the LLM.
  - Never invokes ApprovalGate (Phase 3 will add an optional human gate).
  - Safe to run concurrently with build-flow Commander — they operate on
    different WorkOrders (distinguished by work_order_type) and
    different Celery queues.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phalanx.agents.base import AgentResult, BaseAgent, mark_run_failed
from phalanx.db.models import Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

# Polling settings for the terminal-state loop
_POLL_INTERVAL_SECONDS = 15
_MAX_WAIT_SECONDS = 2700  # 45 min — CI fixes should never take longer

# How many iterations of (techlead → engineer → sre_verify) we'll attempt
# before escalating. Each iteration consumes ~$0.50-1.00 at current pricing
# and ~60-120s latency. 3 is the sweet spot: enough to handle cascading
# failures, not so many that cost balloons on pathological repos.
_MAX_ITERATIONS = 3

# Chain of ceremonial transitions from VERIFYING to SHIPPED. We don't invoke
# approval gates for CI fixes (they're auto-commit), but we respect the state
# machine edges — same pattern build-flow Commander uses.
_POST_VERIFY_CHAIN: list[tuple[str, str]] = [
    ("VERIFYING", "AWAITING_SHIP_APPROVAL"),
    ("AWAITING_SHIP_APPROVAL", "READY_TO_MERGE"),
    ("READY_TO_MERGE", "MERGED"),
    ("MERGED", "RELEASE_PREP"),
    ("RELEASE_PREP", "AWAITING_RELEASE_APPROVAL"),
    ("AWAITING_RELEASE_APPROVAL", "SHIPPED"),
]


@celery_app.task(
    name="phalanx.agents.cifix_commander.execute_run",
    bind=True,
    queue="cifix_commander",
    max_retries=1,
    acks_late=True,
    soft_time_limit=3000,  # 50 min — commander lifetime
    time_limit=3300,
)
def execute_run(
    self, work_order_id: str, project_id: str, run_id: str, **kwargs
) -> dict:  # pragma: no cover
    """Celery entry point. Parallels phalanx.agents.commander.execute_run."""
    agent = CIFixCommanderAgent(
        run_id=run_id, work_order_id=work_order_id, project_id=project_id
    )
    try:
        result = asyncio.run(agent.execute())
    except Exception as exc:
        log.exception("cifix_commander.celery_task_unhandled", run_id=run_id)
        asyncio.run(mark_run_failed(run_id, str(exc)))
        raise
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixCommanderAgent(BaseAgent):
    """Orchestrator for CI Fixer v3 runs. One instance per Run."""

    AGENT_ROLE = "cifix_commander"

    def __init__(
        self,
        run_id: str,
        work_order_id: str,
        project_id: str,
        agent_id: str = "cifix_commander",
    ) -> None:
        super().__init__(run_id=run_id, agent_id=agent_id)
        self.work_order_id = work_order_id
        self.project_id = project_id

    async def execute(self) -> AgentResult:
        self._log.info("cifix_commander.execute.start")

        # ── Phase 1: load WorkOrder + create Run + persist Task DAG ──────────
        async with get_db() as session:
            wo = await self._load_work_order(session)
            if wo is None:
                return AgentResult(
                    success=False,
                    output={},
                    error=f"WorkOrder {self.work_order_id} not found",
                )
            if wo.work_order_type != "ci_fix":
                # Safety: this agent only handles CI-fix work orders.
                return AgentResult(
                    success=False,
                    output={},
                    error=f"WorkOrder {self.work_order_id} is type "
                    f"'{wo.work_order_type}', not 'ci_fix'",
                )

            ci_context = self._parse_ci_context(wo.raw_command)

            await self._create_run(session, wo)
            await self._transition_run("INTAKE", "RESEARCHING")
            await self._audit(
                "state_transition", from_state="INTAKE", to_state="RESEARCHING"
            )

            await self._transition_run("RESEARCHING", "PLANNING")
            await self._persist_initial_dag(session, ci_context)

            # Skip the ApprovalGate invocation — CI fixes are auto-commit.
            # The state-machine edges are still valid (AWAITING_PLAN_APPROVAL is
            # just a state; the gate itself is a separate mechanism).
            await self._transition_run("PLANNING", "AWAITING_PLAN_APPROVAL")
            await self._transition_run("AWAITING_PLAN_APPROVAL", "EXECUTING")

            self._log.info(
                "cifix_commander.dag_persisted_dispatching_advance_run",
                run_id=self.run_id,
                ci_repo=ci_context.get("repo"),
                ci_pr=ci_context.get("pr_number"),
            )

        # ── Phase 2: fire advance_run + iterate until all_green / cap / FAIL ─
        from phalanx.workflow.advance_run import advance_run as advance_run_task

        advance_run_task.apply_async(
            kwargs={"run_id": self.run_id}, queue="commander"
        )

        for _ in range(_MAX_ITERATIONS + 1):  # +1 = the initial pass
            final_status, run_error = await self._poll_for_terminal()

            if final_status in ("FAILED", "CANCELLED"):
                return AgentResult(
                    success=False, output={}, error=run_error or f"Run {final_status}"
                )
            if final_status == "TIMEOUT":
                await mark_run_failed(self.run_id, "cifix_commander timeout")
                return AgentResult(
                    success=False,
                    output={},
                    error="cifix_commander timed out waiting for VERIFYING",
                )

            # final_status == "VERIFYING": advance_run finished an iteration.
            # Read the latest sre_verify verdict to decide: ship, iterate, or fail.
            verdict, verify_output = await self._read_last_sre_verify_verdict()
            iterations_done = await self._count_completed_sre_verifies()

            self._log.info(
                "cifix_commander.iteration_complete",
                iteration=iterations_done,
                verdict=verdict,
                has_verify_output=verify_output is not None,
            )

            if verdict == "all_green":
                return await self._finalize_shipped(ci_context, verify_output)

            # Non-green (or missing verdict — treat as inconclusive). If we
            # still have iterations left, spawn another (techlead, engineer,
            # sre_verify) triple and rewind VERIFYING → EXECUTING.
            if iterations_done >= _MAX_ITERATIONS:
                await self._transition_run(
                    "VERIFYING",
                    "FAILED",
                    error_message=(
                        f"exhausted {_MAX_ITERATIONS} iterations without an "
                        f"all-green CI verify"
                    ),
                )
                return AgentResult(
                    success=False,
                    output={
                        "verdict": "iterations_exhausted",
                        "iterations_used": iterations_done,
                        "last_verify_output": verify_output,
                    },
                    error=f"exhausted {_MAX_ITERATIONS} iterations",
                )

            # Append the next iteration's tasks BEFORE transitioning, so
            # advance_run sees PENDING tasks and doesn't immediately bounce
            # back to VERIFYING. We also forward sre_verify's new_failures
            # into the iteration's ci_context so Tech Lead iteration N+1
            # investigates the right job (not the original failing command,
            # which iteration 1 already fixed).
            iter_ci_context = dict(ci_context)
            if verify_output and verify_output.get("new_failures"):
                iter_ci_context["prior_sre_failures"] = verify_output["new_failures"]
            async with get_db() as session:
                await self._append_iteration_dag(
                    session, iter_ci_context, iterations_done + 1
                )
            await self._transition_run("VERIFYING", "EXECUTING")
            advance_run_task.apply_async(
                kwargs={"run_id": self.run_id}, queue="commander"
            )
            # Loop back to poll the next VERIFYING.

        # Shouldn't fall out here — the loop has an explicit return on each
        # terminal branch — but mark FAILED defensively if we do.
        await mark_run_failed(self.run_id, "cifix_commander loop fell through")
        return AgentResult(
            success=False, output={}, error="cifix_commander loop fell through"
        )

    async def _finalize_shipped(
        self, ci_context: dict, verify_output: dict | None
    ) -> AgentResult:
        """All-green path: walk the SHIP chain to SHIPPED and return success."""
        self._log.info("cifix_commander.verify_chain_start", run_id=self.run_id)
        try:
            for from_s, to_s in _POST_VERIFY_CHAIN:
                await self._transition_run(from_s, to_s)
            self._log.info("cifix_commander.shipped", run_id=self.run_id)
            summary = await self._build_success_summary()
            return AgentResult(
                success=True,
                output={
                    "verdict": "committed",
                    "verify_output": verify_output,
                    **summary,
                },
                tokens_used=0,
            )
        except Exception as exc:
            self._log.error(
                "cifix_commander.post_verify_transition_failed", error=str(exc)
            )
            await mark_run_failed(self.run_id, f"post-verify transition: {exc}")
            return AgentResult(
                success=False,
                output={},
                error=f"post-verify transition failed: {exc}",
            )

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_work_order(self, session: AsyncSession) -> WorkOrder | None:
        result = await session.execute(
            select(WorkOrder).where(WorkOrder.id == self.work_order_id)
        )
        return result.scalar_one_or_none()

    async def _create_run(self, session: AsyncSession, wo: WorkOrder) -> Run:
        """Create the Run row. Parallels build-flow commander._create_or_load_run."""
        from sqlalchemy import func  # noqa: PLC0415

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

    async def _persist_initial_dag(
        self, session: AsyncSession, ci_context: dict
    ) -> None:
        """Insert the v3 iteration-1 DAG: 4 tasks in sequence.

          seq=1  cifix_sre      (sre_mode='setup')  — clone + provision
          seq=2  cifix_techlead                     — investigate
          seq=3  cifix_engineer                     — fix + commit
          seq=4  cifix_sre      (sre_mode='verify') — full CI mimicry

        Each task carries the ci_context in its description so downstream
        agents can read it without re-loading the WorkOrder. The SRE setup
        and verify tasks get different `sre_mode` values inside their
        serialized context.
        """
        repo = ci_context.get("repo") or "?"
        pr = ci_context.get("pr_number")
        job = ci_context.get("failing_job_name") or "?"

        setup_ctx = {**ci_context, "sre_mode": "setup"}
        verify_ctx = {**ci_context, "sre_mode": "verify"}

        setup_task = Task(
            run_id=self.run_id,
            sequence_num=1,
            title=f"Provision sandbox: {repo}#{pr}",
            description=json.dumps(setup_ctx),
            agent_role="cifix_sre",
            status="PENDING",
            estimated_complexity=2,
        )
        techlead = Task(
            run_id=self.run_id,
            sequence_num=2,
            title=f"Investigate CI failure: {repo}#{pr} — {job}",
            description=json.dumps(ci_context),
            agent_role="cifix_techlead",
            status="PENDING",
            estimated_complexity=3,
        )
        engineer = Task(
            run_id=self.run_id,
            sequence_num=3,
            title=f"Apply fix + sandbox verify: {repo}#{pr}",
            description=json.dumps(ci_context),
            agent_role="cifix_engineer",
            status="PENDING",
            estimated_complexity=3,
        )
        verify_task = Task(
            run_id=self.run_id,
            sequence_num=4,
            title=f"Re-run full CI in sandbox: {repo}#{pr}",
            description=json.dumps(verify_ctx),
            agent_role="cifix_sre",
            status="PENDING",
            estimated_complexity=2,
        )
        session.add(setup_task)
        session.add(techlead)
        session.add(engineer)
        session.add(verify_task)
        await session.commit()

    async def _append_iteration_dag(
        self,
        session: AsyncSession,
        ci_context: dict,
        iteration: int,
    ) -> None:
        """Insert the next iteration's tasks: [techlead, engineer, sre_verify].

        We deliberately DO NOT re-run sre_setup — the sandbox container from
        iteration 1 is still alive, still has the repo's deps installed, and
        is exactly the environment we want subsequent edits to target. The
        Engineer's edits are applied inside that same container.

        sequence_num continues from the current max in the Tasks table so
        advance_run walks the new tasks in order without any gap.
        """
        from sqlalchemy import func  # noqa: PLC0415

        repo = ci_context.get("repo") or "?"
        pr = ci_context.get("pr_number")
        job = ci_context.get("failing_job_name") or "?"
        verify_ctx = {**ci_context, "sre_mode": "verify", "iteration": iteration}
        tl_ctx = {**ci_context, "iteration": iteration}

        result = await session.execute(
            select(func.max(Task.sequence_num)).where(Task.run_id == self.run_id)
        )
        current_max = int(result.scalar_one() or 0)

        techlead = Task(
            run_id=self.run_id,
            sequence_num=current_max + 1,
            title=f"[iter {iteration}] Re-investigate after SRE found new failures: {repo}#{pr}",
            description=json.dumps(tl_ctx),
            agent_role="cifix_techlead",
            status="PENDING",
            estimated_complexity=3,
        )
        engineer = Task(
            run_id=self.run_id,
            sequence_num=current_max + 2,
            title=f"[iter {iteration}] Patch follow-up + sandbox verify: {repo}#{pr}",
            description=json.dumps(tl_ctx),
            agent_role="cifix_engineer",
            status="PENDING",
            estimated_complexity=3,
        )
        verify_task = Task(
            run_id=self.run_id,
            sequence_num=current_max + 3,
            title=f"[iter {iteration}] Re-run full CI: {repo}#{pr}",
            description=json.dumps(verify_ctx),
            agent_role="cifix_sre",
            status="PENDING",
            estimated_complexity=2,
        )
        session.add(techlead)
        session.add(engineer)
        session.add(verify_task)
        await session.commit()

    async def _read_last_sre_verify_verdict(
        self,
    ) -> tuple[str | None, dict | None]:
        """Pull the most recent cifix_sre (verify mode) task's verdict + output.

        Returns (verdict, output). verdict is one of 'all_green',
        'new_failures', or None if no verify task has completed yet (which
        is unusual — we only call this on VERIFYING).
        """
        async with get_db() as session:
            result = await session.execute(
                select(Task.output)
                .where(
                    Task.run_id == self.run_id,
                    Task.agent_role == "cifix_sre",
                    Task.status == "COMPLETED",
                )
                .order_by(Task.sequence_num.desc())
            )
            for (output,) in result.all():
                if isinstance(output, dict) and output.get("mode") == "verify":
                    return (output.get("verdict"), output)
        return (None, None)

    async def _count_completed_sre_verifies(self) -> int:
        """How many sre_verify tasks have finished in this run. One per iteration."""
        from sqlalchemy import func  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(
                select(Task.output).where(
                    Task.run_id == self.run_id,
                    Task.agent_role == "cifix_sre",
                    Task.status == "COMPLETED",
                )
            )
            return sum(
                1
                for (output,) in result.all()
                if isinstance(output, dict) and output.get("mode") == "verify"
            )

    async def _poll_for_terminal(self) -> tuple[str, str | None]:
        """Poll the Run until VERIFYING / FAILED / CANCELLED or timeout.

        Returns (final_status, error_message). On timeout returns ("TIMEOUT", None).
        """
        elapsed = 0
        while elapsed < _MAX_WAIT_SECONDS:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS

            async with get_db() as session:
                result = await session.execute(
                    select(Run.status, Run.error_message).where(Run.id == self.run_id)
                )
                row = result.one_or_none()
                if row is None:
                    return ("FAILED", f"Run {self.run_id} vanished during poll")
                status, err = row
                if status in ("VERIFYING", "FAILED", "CANCELLED"):
                    self._log.info(
                        "cifix_commander.poll_terminal",
                        status=status,
                        elapsed_s=elapsed,
                    )
                    return (status, err)
        return ("TIMEOUT", None)

    async def _build_success_summary(self) -> dict:
        """After VERIFYING, pull Engineer's output so caller gets sha/diff info."""
        async with get_db() as session:
            result = await session.execute(
                select(Task.output, Task.id)
                .where(
                    Task.run_id == self.run_id,
                    Task.agent_role == "cifix_engineer",
                )
                .order_by(Task.sequence_num.desc())
                .limit(1)
            )
            row = result.one_or_none()
            if row is None or row[0] is None:
                return {"summary": "no engineer output found"}
            engineer_output = row[0] or {}
            return {
                "commit_sha": engineer_output.get("commit_sha"),
                "files_modified": engineer_output.get("files_modified", []),
                "verify_exit_code": (engineer_output.get("verify") or {}).get(
                    "exit_code"
                ),
                "engineer_task_id": row[1],
            }

    def _parse_ci_context(self, raw_command: str | None) -> dict:
        """WorkOrder.raw_command stores the webhook payload as JSON for ci_fix runs.

        Expected keys: repo, pr_number, branch, sha, failing_job_id,
        failing_job_name, failing_command. Missing keys are tolerated — the
        Tech Lead agent will adapt.
        """
        if not raw_command:
            return {}
        try:
            parsed = json.loads(raw_command)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError as exc:
            self._log.warning("cifix_commander.ci_context_parse_failed", error=str(exc))
            return {}

    async def _audit(self, event: str, **fields) -> None:
        """Lightweight wrapper — BaseAgent may already provide this; no-op fallback."""
        base_audit = getattr(super(), "_audit", None)
        if callable(base_audit):
            await base_audit(event, **fields)
            return
        self._log.info(f"cifix_commander.audit.{event}", **fields)
