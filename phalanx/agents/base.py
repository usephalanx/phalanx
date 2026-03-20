"""
BaseAgent — abstract foundation for all FORGE agents.

Every agent (Commander, Planner, Builder, Reviewer, QA, Security, Release)
inherits from this class. It provides:

  1. Structured logging with run_id / task_id / agent_role context
  2. AuditLog write helper — every action logged to Postgres
  3. Anthropic API client (for planning/reasoning)
  4. Token budget enforcement (hard limit from guardrails)
  5. Retry wrapper with exponential backoff (tenacity)
  6. Abstract `execute()` — subclasses implement this

Design decisions (evidence in EXECUTION_PLAN.md §B):
  AD-001: Builder uses Claude Code SDK subprocess for code; all other agents
          use Anthropic API directly (claude-opus-4-6 by default).
  AD-004: All fault tolerance via Celery task_acks_late + task_reject_on_worker_lost.
          BaseAgent adds tenacity retries for the Anthropic API call layer.
  AP-003: Agents ALWAYS re-raise exceptions after logging — never swallow them.
  AP-004: Every agent uses the Run state machine; never writes status directly.
"""

from __future__ import annotations

import abc
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
import tenacity
from anthropic import Anthropic, APITimeoutError, RateLimitError

from phalanx.config.settings import get_settings

if TYPE_CHECKING:
    from uuid import UUID

log = structlog.get_logger(__name__)

settings = get_settings()

# Anthropic client — shared across all agent instances in the process
_anthropic_client: Anthropic | None = None


def get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,  # tenacity handles retries — avoid double retry
        )
    return _anthropic_client


# Retry policy for Anthropic API calls:
#   - Retries on rate limits and transient network errors
#   - Does NOT retry on auth errors (will loop forever)
_ANTHROPIC_RETRY = tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=60),
    stop=tenacity.stop_after_attempt(settings.anthropic_max_retries),
    retry=tenacity.retry_if_exception_type((APITimeoutError, RateLimitError)),
    reraise=True,
)


class AgentResult:
    """Typed outcome from agent.execute()."""

    def __init__(
        self,
        success: bool,
        output: dict[str, Any],
        tokens_used: int = 0,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.output = output
        self.tokens_used = tokens_used
        self.error = error

    def __repr__(self) -> str:
        return f"AgentResult(success={self.success}, tokens={self.tokens_used})"


class BaseAgent(abc.ABC):
    """
    Abstract base for all FORGE agents.

    Subclasses must implement `execute()`.

    The class provides:
      - `self._log`: bound structlog logger with run/task/agent context
      - `self.claude`: Anthropic client with retry wrapper
      - `self._audit()`: write an AuditLog entry to Postgres
      - `self._transition_run()`: safe state machine transition + DB write
    """

    #: Agent role identifier — must match celery_app task_routes
    AGENT_ROLE: str = "base"

    def __init__(
        self,
        run_id: str | UUID,
        agent_id: str,
        task_id: str | UUID | None = None,
        token_budget: int | None = None,
    ) -> None:
        self.run_id = str(run_id)
        self.task_id = str(task_id) if task_id else None
        self.agent_id = agent_id
        self.token_budget = token_budget or settings.anthropic_max_tokens_default
        self._tokens_used = 0

        self._log = log.bind(
            run_id=self.run_id,
            task_id=self.task_id,
            agent_id=agent_id,
            agent_role=self.AGENT_ROLE,
        )

    @abc.abstractmethod
    async def execute(self) -> AgentResult:
        """
        Core agent logic. Implemented by each subclass.

        Must return an AgentResult. Must NOT catch and swallow exceptions
        from core logic — let them propagate to the Celery task wrapper
        which handles retries and failure transitions (AP-003).
        """

    def _check_budget(self, tokens_requested: int) -> None:
        """Raise if adding tokens_requested would exceed budget."""
        if self._tokens_used + tokens_requested > self.token_budget:
            raise RuntimeError(
                f"Token budget exceeded: used={self._tokens_used} "
                f"requested={tokens_requested} budget={self.token_budget} "
                f"agent={self.agent_id} run={self.run_id}"
            )

    @_ANTHROPIC_RETRY
    def _call_claude(
        self,
        messages: list[dict],
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Call Anthropic API with retry and token tracking.
        Returns the assistant's text response.

        Uses tenacity retry wrapper on RateLimitError / APITimeoutError.
        Auth errors (APIError) propagate immediately.
        """
        self._check_budget(max_tokens)

        response = get_anthropic_client().messages.create(
            model=model or settings.anthropic_model_default,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )

        tokens_used = response.usage.input_tokens + response.usage.output_tokens
        self._tokens_used += tokens_used

        self._log.debug(
            "agent.claude_call",
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=tokens_used,
            budget_remaining=self.token_budget - self._tokens_used,
        )

        return response.content[0].text

    async def _audit(
        self,
        event_type: str,
        from_state: str | None = None,
        to_state: str | None = None,
        tool_name: str | None = None,
        tokens_used: int | None = None,
        duration_ms: int | None = None,
        payload: dict | None = None,
    ) -> None:
        """
        Write an immutable AuditLog entry to Postgres.
        Non-fatal — logs warning on failure but does NOT raise (AP-003 exception:
        audit failures should not abort the agent's core logic).
        """
        try:
            from phalanx.db.models import AuditLog  # noqa: PLC0415
            from phalanx.db.session import get_db  # noqa: PLC0415

            async with get_db() as session:
                entry = AuditLog(
                    run_id=self.run_id,
                    event_type=event_type,
                    agent_role=self.AGENT_ROLE,
                    agent_id=self.agent_id,
                    from_state=from_state,
                    to_state=to_state,
                    tool_name=tool_name,
                    tokens_used=tokens_used or self._tokens_used,
                    duration_ms=duration_ms,
                    payload=payload or {},
                )
                session.add(entry)
                await session.commit()
        except Exception as exc:
            self._log.warning("agent.audit_failed", event_type=event_type, error=str(exc))

    async def _transition_run(
        self,
        from_status: str,
        to_status: str,
        error_message: str | None = None,
        error_context: dict | None = None,
    ) -> None:
        """
        Validate the transition via state machine and persist to Postgres.

        Raises InvalidTransitionError if the transition is not allowed.
        Always logs the transition to AuditLog.
        """
        from sqlalchemy import update  # noqa: PLC0415

        from phalanx.db.models import Run  # noqa: PLC0415
        from phalanx.db.session import get_db  # noqa: PLC0415
        from phalanx.workflow.state_machine import RunStatus, validate_transition  # noqa: PLC0415

        validate_transition(RunStatus(from_status), RunStatus(to_status))

        values: dict = {
            "status": to_status,
            "updated_at": datetime.now(UTC),
        }
        if error_message:
            values["error_message"] = error_message
        if error_context:
            values["error_context"] = error_context

        async with get_db() as session:
            await session.execute(update(Run).where(Run.id == self.run_id).values(**values))
            await session.commit()

        await self._audit(
            event_type="state_transition",
            from_state=from_status,
            to_state=to_status,
            payload={"error_message": error_message} if error_message else {},
        )

        self._log.info(
            "agent.run_transition",
            from_status=from_status,
            to_status=to_status,
        )
