"""
Task Router — maps agent roles to Celery queues and dispatches tasks.

Design decisions:
  - One queue per agent role for priority isolation (matches celery_app.py config).
  - Builder queue is isolated — git operations happen there, never in shared queues.
  - Router is stateless — no DB calls, just queue name lookup + Celery dispatch.
  - Builder uses Claude Code SDK subprocess (see EXECUTION_PLAN.md §B, AD-001).
    All other agents use Anthropic API directly.

Evidence for queue-per-role isolation:
  Celery docs: https://docs.celeryq.dev/en/stable/userguide/routing.html
  Prevents a stuck builder (git clone) from blocking commander tasks.
"""
from __future__ import annotations

import structlog
from celery import Celery

log = structlog.get_logger(__name__)

# Maps agent_role → Celery queue name. Must mirror celery_app.py task_queues.
_ROLE_TO_QUEUE: dict[str, str] = {
    "commander": "commander",
    "planner":   "planner",
    "builder":   "builder",
    "reviewer":  "reviewer",
    "qa":        "qa",
    "security":  "security",
    "release":   "release",
    # Scheduled/ingestion tasks
    "ingestion": "ingestion",
    "skill_drills": "skill_drills",
}

_DEFAULT_QUEUE = "default"

# Maps agent_role → Celery task name for the agent's main execution task.
# These tasks are registered in their respective agent modules (M3+).
_ROLE_TO_TASK: dict[str, str] = {
    "commander": "forge.agents.commander.execute_run",
    "planner":   "forge.agents.planner.execute_task",
    "builder":   "forge.agents.builder.execute_task",
    "reviewer":  "forge.agents.reviewer.execute_task",
    "qa":        "forge.agents.qa.execute_task",
    "security":  "forge.agents.security.execute_task",
    "release":   "forge.agents.release.execute_task",
}


class UnroutableTaskError(ValueError):
    """Raised when no queue mapping exists for the given agent role."""


class TaskRouter:
    """
    Routes tasks to the correct Celery queue based on agent_role.

    Usage:
        router = TaskRouter(celery_app)
        router.dispatch(
            agent_role="builder",
            task_id="uuid",
            run_id="uuid",
            payload={"repo_path": "/tmp/forge-repos/proj"},
        )
    """

    def __init__(self, celery_app: Celery) -> None:
        self._app = celery_app

    def queue_for_role(self, agent_role: str) -> str:
        """Return the queue name for the given agent role."""
        return _ROLE_TO_QUEUE.get(agent_role, _DEFAULT_QUEUE)

    def dispatch(
        self,
        agent_role: str,
        task_id: str,
        run_id: str,
        payload: dict | None = None,
        countdown: int = 0,
        retries: int = 3,
    ) -> str:
        """
        Dispatch a task to the agent's dedicated queue.

        Returns the Celery task ID (async_result.id).

        Raises UnroutableTaskError if the role has no registered task name.
        """
        task_name = _ROLE_TO_TASK.get(agent_role)
        if task_name is None:
            raise UnroutableTaskError(
                f"No Celery task registered for agent_role={agent_role!r}. "
                f"Known roles: {sorted(_ROLE_TO_TASK.keys())}"
            )

        queue = self.queue_for_role(agent_role)
        kwargs = {
            "task_id": task_id,
            "run_id": run_id,
            **(payload or {}),
        }

        result = self._app.send_task(
            task_name,
            kwargs=kwargs,
            queue=queue,
            countdown=countdown,
            max_retries=retries,
        )

        log.info(
            "task_router.dispatched",
            agent_role=agent_role,
            celery_task_id=result.id,
            task_id=task_id,
            run_id=run_id,
            queue=queue,
        )

        return result.id
