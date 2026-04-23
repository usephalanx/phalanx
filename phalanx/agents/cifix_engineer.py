"""CI Fixer v3 — Engineer agent (implementer).

Phase 1 STUB. Real implementation coming next pass.

Role (planned):
  - Reads the latest cifix_techlead Task.output.fix_spec from the same run_id.
  - Uses Sonnet 4.6 for tight code edits via replace_in_file.
  - Runs the exact failing command in sandbox to verify locally.
  - Writes Task.output = {diff, files_modified, verify: {cmd, exit_code}}.
  - Does NOT investigate; does NOT second-guess the spec.
  - Single-pass Celery task (no internal loop).
"""

from __future__ import annotations

import asyncio

import structlog

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(
    name="phalanx.agents.cifix_engineer.execute_task",
    bind=True,
    max_retries=1,
    soft_time_limit=900,
    time_limit=1020,
)
def execute_task(self, task_id: str, run_id: str, **kwargs) -> dict:
    """Celery entry point. STUB."""
    agent = CIFixEngineerAgent(run_id=run_id, agent_id="cifix_engineer", task_id=task_id)
    result = asyncio.run(agent.execute())
    return {"success": result.success, "output": result.output, "error": result.error}


class CIFixEngineerAgent(BaseAgent):
    """STUB — returns a fake diff payload so SRE stub has something to read."""

    AGENT_ROLE = "cifix_engineer"

    async def execute(self) -> AgentResult:
        self._log.info("cifix_engineer.stub.execute", run_id=self.run_id)
        return AgentResult(
            success=True,
            output={
                "stub": True,
                "agent": "cifix_engineer",
                "diff": "",
                "files_modified": [],
                "verify": {"cmd": "", "exit_code": None},
            },
            tokens_used=0,
        )
