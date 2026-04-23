"""CI Fixer v3 — SRE agent (infra + full CI mimicry).

Phase 1 STUB. Real implementation lands in Phase 2 (not active in Phase 1 MVP).

Role (planned, Phase 2):
  - Reads the latest cifix_engineer Task.output.diff.
  - Parses .github/workflows/*.yml to enumerate all CI jobs.
  - Runs each relevant job command in sandbox; reports which pass/fail.
  - Owns sandbox infra: can call upgrade_sandbox_tool(tool, min_version)
    when it detects env mismatches (e.g., stale ruff vs modern pyproject.toml).
  - Writes Task.output = {verdict: 'all_green' | 'new_failures',
                          jobs: [{name, exit_code, tail}],
                          new_failures: [...], infra_changes: [...]}.
  - Does NOT patch application code.
"""

from __future__ import annotations

import asyncio

import structlog

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(
    name="phalanx.agents.cifix_sre.execute_task",
    bind=True,
    max_retries=1,
    soft_time_limit=1200,
    time_limit=1320,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:
    """Celery entry point. STUB — Phase 2 will flesh out."""
    agent = CIFixSREAgent(run_id=run_id, agent_id="cifix_sre", task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixSREAgent(BaseAgent):
    """STUB — returns verdict='all_green' so Phase 1 single-pass runs exit cleanly."""

    AGENT_ROLE = "cifix_sre"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_sre.stub.execute", run_id=self.run_id)
        return AgentResult(
            success=True,
            output={
                "stub": True,
                "agent": "cifix_sre",
                "verdict": "all_green",
                "jobs": [],
                "new_failures": [],
                "infra_changes": [],
            },
            tokens_used=0,
        )
