"""CI Fixer v3 — Tech Lead agent (investigator).

Phase 1 STUB. Real implementation coming next pass.

Role (planned):
  - Reads CI log, PR diff, git blame, repo files.
  - Uses GPT-5.4 for root-cause reasoning + research from training data.
  - Writes a fix specification to Task.output: {root_cause, affected_files,
    fix_spec, confidence, open_questions}. NO code edits. NO sandbox.
  - Single-pass Celery task (no internal loop).
"""

from __future__ import annotations

import asyncio

import structlog

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(
    name="phalanx.agents.cifix_techlead.execute_task",
    bind=True,
    max_retries=1,
    soft_time_limit=600,
    time_limit=720,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:
    """Celery entry point. STUB."""
    agent = CIFixTechLeadAgent(run_id=run_id, agent_id="cifix_techlead", task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixTechLeadAgent(BaseAgent):
    """STUB — returns a fake fix_spec so downstream stubs have something to read."""

    AGENT_ROLE = "cifix_techlead"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_techlead.stub.execute", run_id=self.run_id)
        return AgentResult(
            success=True,
            output={
                "stub": True,
                "agent": "cifix_techlead",
                "root_cause": "(stub — real investigation not yet implemented)",
                "affected_files": [],
                "fix_spec": "(stub)",
                "confidence": 0.0,
                "open_questions": [],
            },
            tokens_used=0,
        )
