"""CI Fixer v3 — Commander agent (orchestrator).

Phase 1 STUB. Real implementation coming next pass.

Role:
  - Entry point for CI-failure-driven runs (when ci_integrations.cifixer_version='v3').
  - Creates the WorkOrder(work_order_type='ci_fix') + Run + initial Task DAG
    (techlead → engineer → sre) and fires advance_run_task.
  - Polls Run for VERIFYING; on verdict='new_failures' inserts next iteration's
    Tasks and transitions VERIFYING → EXECUTING (bounded by MAX_ITERATIONS).
  - Never reads code, never calls sandbox. Pure coordinator.

v2 CI Fixer continues to handle ci_integrations.cifixer_version='v2' — this
module does NOT touch that path.
"""

from __future__ import annotations

import asyncio

import structlog

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(
    name="phalanx.agents.cifix_commander.execute_run",
    bind=True,
    max_retries=1,
    soft_time_limit=7200,  # 2h — matches build-flow commander budget
    time_limit=7500,
)
def execute_run(self, task_id: str, run_id: str, **kwargs) -> dict:
    """Celery entry point. STUB — returns success with stub marker."""
    agent = CIFixCommanderAgent(run_id=run_id, agent_id="cifix_commander", task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixCommanderAgent(BaseAgent):
    """STUB — returns immediately. Real orchestration logic lands in the next pass."""

    AGENT_ROLE = "cifix_commander"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_commander.stub.execute", run_id=self.run_id)
        return AgentResult(
            success=True,
            output={"stub": True, "phase": 1, "agent": "cifix_commander"},
            tokens_used=0,
        )
