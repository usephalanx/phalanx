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
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from phalanx.agents.base import AgentResult, BaseAgent, mark_run_failed
from phalanx.db.models import CIIntegration, Run, Task, WorkOrder
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# Polling settings for the terminal-state loop
_POLL_INTERVAL_SECONDS = 15
_MAX_WAIT_SECONDS = 2700  # 45 min — CI fixes should never take longer

# How many iterations of (techlead → engineer → sre_verify) we'll attempt
# before escalating. Each iteration consumes ~$0.50-1.00 at current pricing
# and ~60-120s latency. 3 is the sweet spot: enough to handle cascading
# failures, not so many that cost balloons on pathological repos.
_MAX_ITERATIONS = 3

# v1.6 Phase 2 — per-run cost cap. Aggregate `tasks.tokens_used` per run;
# abort dispatch of any further iteration if the running estimate exceeds
# the cap. Conservative blended rate covers GPT-5.4 input+output + Sonnet
# input+output.
#
# v1.7 — bumped from $1 to $30 to accommodate Challenger ($5) + reasonable
# multi-iteration headroom. Per-task caps remain in each agent's local
# config (TL=$5, Challenger=$5, SRE=$4, Engineer=$1).
_COST_PER_TOKEN_USD: float = 20e-6  # blended; conservative (real avg ~$10/1M)
_MAX_RUN_COST_USD: float = 30.00    # hard abort above this

# v1.7.2.3 — wall-clock cap. The polling loop already has _MAX_WAIT_SECONDS
# (45 min) per advance_run wait, but a pathological run that bounces between
# VERIFYING and EXECUTING can chew clock without a per-task hang. This caps
# total run wall-clock from the first commander tick.
_MAX_RUN_RUNTIME_SECONDS: int = 1800  # 30 min

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
        # v1.7.2.3 — wall-clock anchor for the runtime cap. Set on first
        # tick of execute() so retries (where execute() may be called
        # again) reset the clock.
        self._run_started_monotonic: float | None = None

    async def execute(self) -> AgentResult:
        import time  # noqa: PLC0415

        self._run_started_monotonic = time.monotonic()
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

            run = await self._create_or_load_run(session, wo)

            # Idempotent setup — if celery retried after the DAG was already
            # persisted + transitions happened, skip the ceremony. Transitions
            # are NOT safe to repeat (validate_transition would reject e.g.
            # VERIFYING → RESEARCHING), so this guard is for correctness.
            if run.status == "INTAKE":
                # _transition_run already writes an AuditLog row internally;
                # an explicit _audit() here would be a duplicate. (Caught in
                # the post-canary cleanup pass.)
                await self._transition_run("INTAKE", "RESEARCHING")
                await self._transition_run("RESEARCHING", "PLANNING")
                await self._persist_initial_dag(session, ci_context)
                # Skip the ApprovalGate invocation — CI fixes are auto-commit.
                # State-machine edges are still valid (AWAITING_PLAN_APPROVAL
                # is just a state; the gate itself is a separate mechanism).
                await self._transition_run("PLANNING", "AWAITING_PLAN_APPROVAL")
                await self._transition_run("AWAITING_PLAN_APPROVAL", "EXECUTING")
                self._log.info(
                    "cifix_commander.dag_persisted_dispatching_advance_run",
                    run_id=self.run_id,
                    ci_repo=ci_context.get("repo"),
                    ci_pr=ci_context.get("pr_number"),
                )
            else:
                # Retry path: DAG + transitions already happened on the
                # previous attempt. Just resume polling.
                self._log.info(
                    "cifix_commander.retry_resuming",
                    run_id=self.run_id,
                    current_status=run.status,
                )

        # ── Phase 2: fire advance_run + iterate until all_green / cap / FAIL ─
        from phalanx.workflow.advance_run import advance_run as advance_run_task

        advance_run_task.apply_async(
            kwargs={"run_id": self.run_id}, queue="commander"
        )

        # Each loop pass consumes ONE completed sre_verify (iteration N's):
        # pass 1 reads iter-1's verdict (always present from the initial DAG),
        # pass N reads iter-N's verdict after appending. Hitting
        # iterations_done >= _MAX_ITERATIONS terminates via the FAILED branch
        # below — the range cap is a defense-in-depth belt-and-braces.
        for _ in range(_MAX_ITERATIONS):
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
                # v1.7.2.3 — sha-mismatch gate. SRE verify reports a green,
                # but if the verified_commit_sha doesn't match what engineer
                # pushed, we may have verified the wrong code. Reject as
                # untrusted-green and treat as a verification failure.
                eng_sha = (verify_output or {}).get("engineer_commit_sha")
                ver_sha = (verify_output or {}).get("verified_commit_sha")
                if eng_sha and ver_sha and eng_sha != ver_sha:
                    self._log.warning(
                        "cifix_commander.green_rejected_sha_mismatch",
                        run_id=self.run_id,
                        engineer_commit_sha=eng_sha,
                        verified_commit_sha=ver_sha,
                    )
                    await self._transition_run(
                        "VERIFYING",
                        "FAILED",
                        error_message=(
                            f"untrusted_green: verified_commit_sha {ver_sha[:12]} "
                            f"!= engineer_commit_sha {eng_sha[:12]}"
                        ),
                    )
                    escalation = await self._build_and_persist_escalation(
                        final_reason="untrusted_green_sha_mismatch"
                    )
                    return AgentResult(
                        success=False,
                        output={
                            "verdict": "untrusted_green_sha_mismatch",
                            "engineer_commit_sha": eng_sha,
                            "verified_commit_sha": ver_sha,
                            "escalation_record": escalation,
                        },
                        error="untrusted_green_sha_mismatch",
                    )

                # v1.7.2.4 — full-CI re-confirm gate. SRE Verify ran TL's
                # narrow verify_command and reported all_green, but TL may
                # have targeted the wrong failing job (coverage cell shape)
                # OR engineer's edit may have broken a previously-green
                # check (flake cell shape). Poll GitHub's check-runs on
                # the engineer head sha, compare to base, refuse to ship
                # if the full CI surface isn't actually green.
                gate_verdict = await self._run_check_gate(
                    ci_context=ci_context, head_sha=eng_sha
                )
                if gate_verdict is None:
                    # No integration / no token — gate cannot run. Fall
                    # through to ship per legacy v1.7.2.3 behavior. Logged
                    # so prod operator notices the missing config.
                    self._log.warning(
                        "cifix_commander.check_gate_skipped_no_integration",
                        run_id=self.run_id,
                    )
                    return await self._finalize_shipped(ci_context, verify_output)
                self._log.info(
                    "cifix_commander.check_gate_verdict",
                    run_id=self.run_id,
                    decision=gate_verdict.decision,
                    fixed=gate_verdict.fixed,
                    regressed=gate_verdict.regressed,
                    still_failing=gate_verdict.still_failing,
                    pending=gate_verdict.pending,
                    poll_seconds=gate_verdict.poll_seconds,
                )
                if gate_verdict.decision == "TRUE_GREEN":
                    enriched_output = dict(verify_output or {})
                    enriched_output["check_gate"] = gate_verdict.to_dict()
                    return await self._finalize_shipped(ci_context, enriched_output)

                # NOT_FIXED → REPLAN if we still have headroom (SRE Verify
                # told us "fixed" but GitHub disagrees; another iteration
                # might pick the right failing job).
                if gate_verdict.decision == "NOT_FIXED":
                    if iterations_done < _MAX_ITERATIONS:
                        # Synthesize a SRE-failure-equivalent payload so the
                        # existing iteration logic picks it up. TL on the
                        # next iter will see prior_sre_failures with the
                        # GitHub check details — strong replan signal.
                        synthesized = self._gate_failures_as_sre_failures(gate_verdict)
                        verdict = "new_failures"
                        verify_output = dict(verify_output or {})
                        verify_output["new_failures"] = synthesized
                        verify_output["check_gate"] = gate_verdict.to_dict()
                        # Fall through to the standard iteration path below.
                    else:
                        await self._transition_run(
                            "VERIFYING",
                            "FAILED",
                            error_message=(
                                f"check_gate_not_fixed_after_{iterations_done}_iters: "
                                f"{gate_verdict.notes}"
                            ),
                        )
                        escalation = await self._build_and_persist_escalation(
                            final_reason="check_gate_not_fixed"
                        )
                        return AgentResult(
                            success=False,
                            output={
                                "verdict": "check_gate_not_fixed",
                                "iterations_used": iterations_done,
                                "check_gate": gate_verdict.to_dict(),
                                "escalation_record": escalation,
                            },
                            error="check_gate_not_fixed",
                        )

                # REGRESSION / PENDING_TIMEOUT / MISSING_DATA → ESCALATE.
                # Engineer broke something previously-green (regression),
                # or GitHub never settled (timeout), or no checks reported
                # (missing). Don't loop; surface to a human.
                if gate_verdict.decision in ("REGRESSION", "PENDING_TIMEOUT", "MISSING_DATA"):
                    final_reason = f"check_gate_{gate_verdict.decision.lower()}"
                    await self._transition_run(
                        "VERIFYING",
                        "FAILED",
                        error_message=f"{final_reason}: {gate_verdict.notes}",
                    )
                    escalation = await self._build_and_persist_escalation(
                        final_reason=final_reason
                    )
                    return AgentResult(
                        success=False,
                        output={
                            "verdict": final_reason,
                            "iterations_used": iterations_done,
                            "check_gate": gate_verdict.to_dict(),
                            "escalation_record": escalation,
                        },
                        error=final_reason,
                    )

            # v1.7.2.3 — runtime cap. Stop pathological runs that bounce
            # between states for too long. _MAX_WAIT_SECONDS caps each
            # poll cycle; this caps the whole run.
            if self._run_started_monotonic is not None:
                import time  # noqa: PLC0415

                elapsed_s = time.monotonic() - self._run_started_monotonic
                if elapsed_s > _MAX_RUN_RUNTIME_SECONDS:
                    self._log.warning(
                        "cifix_commander.runtime_cap_exceeded",
                        run_id=self.run_id,
                        elapsed_s=int(elapsed_s),
                        cap_s=_MAX_RUN_RUNTIME_SECONDS,
                    )
                    await self._transition_run(
                        "VERIFYING",
                        "FAILED",
                        error_message=(
                            f"runtime_cap_exceeded: {int(elapsed_s)}s > "
                            f"{_MAX_RUN_RUNTIME_SECONDS}s"
                        ),
                    )
                    escalation = await self._build_and_persist_escalation(
                        final_reason="runtime_cap_exceeded"
                    )
                    return AgentResult(
                        success=False,
                        output={
                            "verdict": "runtime_cap_exceeded",
                            "iterations_used": iterations_done,
                            "elapsed_s": int(elapsed_s),
                            "escalation_record": escalation,
                        },
                        error="runtime_cap_exceeded",
                    )

            # v1.7.2.3 — no-progress gate. If the last two verify failures
            # have the SAME fingerprint (same command, same exit, same
            # normalized output), the engineer's patches aren't moving
            # the needle. Stop iterating instead of burning more tokens.
            fps = await self._collect_verify_fingerprints()
            from phalanx.agents._failure_fingerprint import is_repeated  # noqa: PLC0415

            if is_repeated(fps):
                self._log.warning(
                    "cifix_commander.no_progress_detected",
                    run_id=self.run_id,
                    fingerprints=fps[-3:],
                )
                await self._transition_run(
                    "VERIFYING",
                    "FAILED",
                    error_message=(
                        f"no_progress_detected: fingerprint {fps[-1]} "
                        f"repeated across iterations"
                    ),
                )
                escalation = await self._build_and_persist_escalation(
                    final_reason="no_progress_detected"
                )
                return AgentResult(
                    success=False,
                    output={
                        "verdict": "no_progress_detected",
                        "iterations_used": iterations_done,
                        "fingerprint_history": fps,
                        "last_verify_output": verify_output,
                        "escalation_record": escalation,
                    },
                    error="no_progress_detected",
                )

            # v1.6 Phase 2 — per-run cost cap. Check BEFORE deciding to
            # dispatch another iteration. The MAX_ITERATIONS guard caps
            # COUNT; this caps SPEND. Both must hold.
            should_abort, estimate, total_tokens = await self._check_cost_cap()
            if should_abort:
                self._log.warning(
                    "cifix_commander.cost_cap_exceeded",
                    run_id=self.run_id,
                    tokens=total_tokens,
                    estimate_usd=round(estimate, 3),
                    cap_usd=_MAX_RUN_COST_USD,
                )
                await self._transition_run(
                    "VERIFYING",
                    "FAILED",
                    error_message=(
                        f"cost_cap_exceeded: ~${estimate:.2f} > ${_MAX_RUN_COST_USD} "
                        f"({total_tokens} tokens)"
                    ),
                )
                escalation = await self._build_and_persist_escalation(
                    final_reason="cost_cap_exceeded"
                )
                return AgentResult(
                    success=False,
                    output={
                        "verdict": "cost_cap_exceeded",
                        "iterations_used": iterations_done,
                        "tokens_used": total_tokens,
                        "estimated_cost_usd": round(estimate, 3),
                        "escalation_record": escalation,
                    },
                    error=f"cost_cap_exceeded: ~${estimate:.2f} > ${_MAX_RUN_COST_USD}",
                )

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
                escalation = await self._build_and_persist_escalation(
                    final_reason="iterations_exhausted"
                )
                return AgentResult(
                    success=False,
                    output={
                        "verdict": "iterations_exhausted",
                        "iterations_used": iterations_done,
                        "last_verify_output": verify_output,
                        "escalation_record": escalation,
                    },
                    error=f"exhausted {_MAX_ITERATIONS} iterations",
                )

            # Atomic append + transition. A stray advance_run tick between
            # these two ops (fired via _schedule_recheck after iteration N's
            # sre_verify completed) could otherwise observe
            #   status=VERIFYING + all-old-tasks-COMPLETED + new-tasks-PENDING
            # and dispatch a new techlead task against status=VERIFYING. We
            # commit both writes in a single transaction so advance_run's DB
            # read is never inconsistent.
            iter_ci_context = dict(ci_context)
            if verify_output and verify_output.get("new_failures"):
                iter_ci_context["prior_sre_failures"] = verify_output["new_failures"]
            async with get_db() as session:
                await self._append_iteration_and_transition(
                    session=session,
                    ci_context=iter_ci_context,
                    iteration=iterations_done + 1,
                )
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

    async def _create_or_load_run(self, session: AsyncSession, wo: WorkOrder) -> Run:
        """Idempotent Run creation. If celery retries this task, the second
        attempt finds an existing Run row and returns it without raising.
        Parallels build-flow commander._create_or_load_run."""
        from sqlalchemy import func  # noqa: PLC0415

        existing = await session.execute(select(Run).where(Run.id == self.run_id))
        existing_run = existing.scalar_one_or_none()
        if existing_run is not None:
            self._log.info("cifix_commander.run_already_exists", status=existing_run.status)
            return existing_run

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
        """Insert the v3 iteration-1 DAG.

        v1.7 — 5 tasks (Challenger added between TL and Engineer in shadow
        mode; verdict is logged but does NOT gate downstream dispatch yet):

          seq=1  cifix_sre        (sre_mode='setup')   — clone + provision
          seq=2  cifix_techlead                        — investigate
          seq=3  cifix_challenger                      — adversarial review (NEW, shadow mode)
          seq=4  cifix_engineer                        — fix + commit
          seq=5  cifix_sre        (sre_mode='verify')  — full CI mimicry

        Each task carries the ci_context in its description so downstream
        agents can read it without re-loading the WorkOrder. The SRE setup
        and verify tasks get different `sre_mode` values inside their
        serialized context. The Challenger inherits the SRE setup's
        workspace via DB lookup (see CIFixChallengerAgent._load_ctx_payload)
        and reads TL's output via DB lookup at execute time.
        """
        repo = ci_context.get("repo") or "?"
        pr = ci_context.get("pr_number")
        job = ci_context.get("failing_job_name") or "?"

        setup_ctx = {**ci_context, "sre_mode": "setup"}
        verify_ctx = {**ci_context, "sre_mode": "verify"}
        # Challenger needs the workspace_path which the SRE setup task
        # produces in its output. Commander's poll loop already inherits
        # workspace_path via _load_sre_setup_output pattern; Challenger
        # mirrors that. ci_log_text is read by the Challenger from the
        # TL task's tool history (fetch_ci_log result).
        challenger_ctx = {**ci_context, "shadow_mode": True}

        setup_task = Task(
            run_id=self.run_id,
            sequence_num=1,
            title=f"Provision sandbox: {repo}#{pr}",
            description=json.dumps(setup_ctx),
            agent_role="cifix_sre_setup",  # v1.7 — was "cifix_sre"
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
        challenger = Task(
            run_id=self.run_id,
            sequence_num=3,
            title=f"Adversarial review of fix plan: {repo}#{pr} (shadow mode)",
            description=json.dumps(challenger_ctx),
            agent_role="cifix_challenger",
            status="PENDING",
            estimated_complexity=2,
        )
        engineer = Task(
            run_id=self.run_id,
            sequence_num=4,
            title=f"Apply fix + sandbox verify: {repo}#{pr}",
            description=json.dumps(ci_context),
            agent_role="cifix_engineer",
            status="PENDING",
            estimated_complexity=3,
        )
        verify_task = Task(
            run_id=self.run_id,
            sequence_num=5,
            title=f"Re-run full CI in sandbox: {repo}#{pr}",
            description=json.dumps(verify_ctx),
            agent_role="cifix_sre_verify",  # v1.7 — was "cifix_sre"
            status="PENDING",
            estimated_complexity=2,
        )
        session.add(setup_task)
        session.add(techlead)
        session.add(challenger)
        session.add(engineer)
        session.add(verify_task)
        await session.commit()

    async def _append_iteration_and_transition(
        self,
        session: AsyncSession,
        ci_context: dict,
        iteration: int,
    ) -> None:
        """Insert iteration tasks AND transition VERIFYING → EXECUTING in a
        single DB transaction. Guards the race where advance_run could
        observe a half-applied state (new PENDING tasks + status=VERIFYING)
        and dispatch an agent task against the wrong state.
        """
        from sqlalchemy import func, update  # noqa: PLC0415

        from phalanx.db.models import Run  # noqa: PLC0415
        from phalanx.workflow.state_machine import (  # noqa: PLC0415
            RunStatus,
            validate_transition,
        )

        # Validate the transition using the same state machine the rest of
        # the codebase uses. This raises InvalidTransitionError before we
        # write any tasks, which is what we want.
        validate_transition(RunStatus.VERIFYING, RunStatus.EXECUTING)

        repo = ci_context.get("repo") or "?"
        pr = ci_context.get("pr_number")
        verify_ctx = {**ci_context, "sre_mode": "verify", "iteration": iteration}
        tl_ctx = {**ci_context, "iteration": iteration}

        result = await session.execute(
            select(func.max(Task.sequence_num)).where(Task.run_id == self.run_id)
        )
        current_max = int(result.scalar_one() or 0)

        session.add(
            Task(
                run_id=self.run_id,
                sequence_num=current_max + 1,
                title=f"[iter {iteration}] Re-investigate after SRE found new failures: {repo}#{pr}",
                description=json.dumps(tl_ctx),
                agent_role="cifix_techlead",
                status="PENDING",
                estimated_complexity=3,
            )
        )
        session.add(
            Task(
                run_id=self.run_id,
                sequence_num=current_max + 2,
                title=f"[iter {iteration}] Patch follow-up + sandbox verify: {repo}#{pr}",
                description=json.dumps(tl_ctx),
                agent_role="cifix_engineer",
                status="PENDING",
                estimated_complexity=3,
            )
        )
        session.add(
            Task(
                run_id=self.run_id,
                sequence_num=current_max + 3,
                title=f"[iter {iteration}] Re-run full CI: {repo}#{pr}",
                description=json.dumps(verify_ctx),
                agent_role="cifix_sre_verify",  # v1.7 — was "cifix_sre"
                status="PENDING",
                estimated_complexity=2,
            )
        )

        # Flip the run status in the SAME transaction. A raw UPDATE so the
        # commit picks up both the task inserts and the status flip.
        await session.execute(
            update(Run).where(Run.id == self.run_id).values(status="EXECUTING")
        )
        await session.commit()
        self._log.info(
            "cifix_commander.iteration_appended_atomically",
            iteration=iteration,
            added_seq_range=(current_max + 1, current_max + 3),
        )

    async def _check_cost_cap(self) -> tuple[bool, float, int]:
        """Returns (should_abort, current_estimate_usd, total_tokens).

        Aggregates tasks.tokens_used for this run; returns abort=True if
        estimate > _MAX_RUN_COST_USD. Caller is responsible for transitioning
        Run.status to FAILED with a structured error_message on abort.
        """
        from sqlalchemy import func  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(Task.tokens_used), 0)).where(
                    Task.run_id == self.run_id
                )
            )
            total_tokens = int(result.scalar() or 0)
        estimate = total_tokens * _COST_PER_TOKEN_USD
        return (estimate > _MAX_RUN_COST_USD, estimate, total_tokens)

    async def _read_last_sre_verify_verdict(
        self,
    ) -> tuple[str | None, dict | None]:
        """Pull the most recent cifix_sre (verify mode) task's verdict + output.

        Returns (verdict, output). verdict is one of 'all_green',
        'new_failures', or None if no verify task has completed yet (which
        is unusual — we only call this on VERIFYING).
        """
        # v1.7 broadened the SRE role names; match all three to keep
        # commander-vs-agent forward/backward compat during the cutover.
        async with get_db() as session:
            result = await session.execute(
                select(Task.output)
                .where(
                    Task.run_id == self.run_id,
                    Task.agent_role.in_(
                        ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                    ),
                    Task.status == "COMPLETED",
                )
                .order_by(Task.sequence_num.desc())
            )
            for (output,) in result.all():
                if isinstance(output, dict) and output.get("mode") == "verify":
                    return (output.get("verdict"), output)
        return (None, None)

    async def _load_integration_for_repo(
        self, repo: str | None
    ) -> "CIIntegration | None":
        """v1.7.2.4 — fetch the CIIntegration row for the gate's GitHub API
        access. Returns None if no row (gate then falls back to legacy
        ship-on-narrow-verify behavior with a warning log)."""
        if not repo:
            return None
        async with get_db() as session:
            result = await session.execute(
                select(CIIntegration).where(CIIntegration.repo_full_name == repo)
            )
            return result.scalar_one_or_none()

    async def _run_check_gate(
        self, *, ci_context: dict, head_sha: str | None
    ):  # returns CheckGateVerdict | None
        """v1.7.2.4 — full-CI re-confirm gate. Polls GitHub's check-runs on
        head_sha and compares to ci_context.sha (the failing-CI sha at run
        start). Returns the CheckGateVerdict, or None if the gate cannot run
        (no integration, no token, no head_sha)."""
        if not head_sha:
            return None
        repo = ci_context.get("repo")
        base_sha = ci_context.get("sha")
        if not (repo and base_sha):
            return None
        integration = await self._load_integration_for_repo(repo)
        if integration is None or not integration.github_token:
            return None

        from phalanx.agents._github_check_gate import evaluate_check_gate  # noqa: PLC0415
        from phalanx.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        # Caps: poll_timeout 5 min, interval 15s. Tunable later if humanize
        # or other slow-CI repos need longer.
        try:
            return await evaluate_check_gate(
                repo=repo,
                github_token=integration.github_token,
                base_sha=base_sha,
                head_sha=head_sha,
                poll_timeout_s=int(getattr(settings, "ci_fixer_check_gate_timeout_s", 300)),
                poll_interval_s=int(getattr(settings, "ci_fixer_check_gate_interval_s", 15)),
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "cifix_commander.check_gate_exception",
                run_id=self.run_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

    @staticmethod
    def _gate_failures_as_sre_failures(gate_verdict) -> list[dict]:
        """v1.7.2.4 — flatten the gate's failure detail into the same shape
        SRE Verify produces in `new_failures`, so the existing iteration
        path (which appends prior_sre_failures into TL's next ci_context)
        picks them up unchanged."""
        out: list[dict] = []
        for name in (gate_verdict.regressed or []) + (gate_verdict.still_failing or []):
            cs = gate_verdict.post_checks.get(name)
            if cs is None:
                continue
            out.append({
                "name": name,
                "cmd": f"github_check_run:{name}",  # synthetic — TL reads as text
                "exit_code": 1,
                "stderr_tail": "",
                "stdout_tail": cs.summary or "",
                "html_url": cs.html_url,
                "conclusion": cs.conclusion,
                "source": "check_gate",
            })
        return out

    async def _build_and_persist_escalation(self, *, final_reason: str) -> dict:
        """v1.7.2.3 — build the structured escalation record from this run's
        tasks, persist to `runs.error_context`, return it for inclusion in
        the AgentResult.output. Idempotent (safe to call multiple times
        on the same run; last write wins).
        """
        from phalanx.agents._escalation_record import build_escalation_record  # noqa: PLC0415
        from sqlalchemy import update  # noqa: PLC0415

        async with get_db() as session:
            result = await session.execute(
                select(
                    Task.sequence_num, Task.agent_role, Task.status,
                    Task.output, Task.error,
                )
                .where(Task.run_id == self.run_id)
                .order_by(Task.sequence_num.asc())
            )
            rows = [
                {
                    "sequence_num": r[0],
                    "agent_role": r[1],
                    "status": r[2],
                    "output": r[3],
                    "error": r[4],
                }
                for r in result.all()
            ]
            record = build_escalation_record(final_reason=final_reason, tasks=rows)

            try:
                await session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(error_context=record)
                )
                await session.commit()
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "cifix_commander.escalation_persist_failed", error=str(exc)
                )
            return record

    async def _collect_verify_fingerprints(self) -> list[str]:
        """v1.7.2.3 — sequence of `fingerprint` values from each completed
        sre_verify task in this run, ordered by sequence_num.

        Used by the no-progress gate. Empty/missing fingerprints are
        skipped (safe — no false-positive repeats).
        """
        async with get_db() as session:
            result = await session.execute(
                select(Task.output)
                .where(
                    Task.run_id == self.run_id,
                    Task.agent_role.in_(
                        ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                    ),
                    Task.status == "COMPLETED",
                )
                .order_by(Task.sequence_num.asc())
            )
            fps: list[str] = []
            for (output,) in result.all():
                if not isinstance(output, dict):
                    continue
                if output.get("mode") != "verify":
                    continue
                fp = output.get("fingerprint")
                if isinstance(fp, str) and fp:
                    fps.append(fp)
            return fps

    async def _count_completed_sre_verifies(self) -> int:
        """How many sre_verify tasks have finished in this run. One per iteration."""

        async with get_db() as session:
            result = await session.execute(
                select(Task.output).where(
                    Task.run_id == self.run_id,
                    Task.agent_role.in_(
                        ["cifix_sre", "cifix_sre_setup", "cifix_sre_verify"]
                    ),
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

