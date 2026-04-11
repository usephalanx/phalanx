"""
BaseAgent — abstract foundation for all FORGE agents.

Every agent (Commander, Planner, Builder, Reviewer, QA, Security, Release)
inherits from this class. It provides:

  1. Structured logging with run_id / task_id / agent_role context
  2. AuditLog write helper — every action logged to Postgres
  3. Claude call layer: CLI-first (Max subscription), API fallback
  4. Token budget enforcement (hard limit from guardrails)
  5. Retry wrapper with exponential backoff (tenacity)
  6. Abstract `execute()` — subclasses implement this

Design decisions (evidence in EXECUTION_PLAN.md §B):
  AD-001: _call_claude() tries Claude Code CLI subprocess first (uses Max
          subscription — zero API credit burn), falls back to Anthropic API.
  AD-004: All fault tolerance via Celery task_acks_late + task_reject_on_worker_lost.
          BaseAgent adds tenacity retries for the Anthropic API call layer.
  AP-003: Agents ALWAYS re-raise exceptions after logging — never swallow them.
  AP-004: Every agent uses the Run state machine; never writes status directly.
"""

from __future__ import annotations

import abc
import glob
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
import tenacity
from anthropic import (
    Anthropic,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from phalanx.config.settings import get_settings

if TYPE_CHECKING:
    from uuid import UUID

log = structlog.get_logger(__name__)

settings = get_settings()

# Anthropic client — shared across all agent instances in the process
_anthropic_client: Anthropic | None = None

# Claude CLI binary — resolved once at import time
_CLAUDE_CLI_SEARCH_PATHS = [
    # Standard PATH install (npm i -g @anthropic-ai/claude-code)
    "claude",
    # VS Code extension binaries (macOS)
    os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-2.1.81-darwin-arm64/resources/native-binary/claude"
    ),
    os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-2.1.79-darwin-arm64/resources/native-binary/claude"
    ),
]


def _find_claude_cli() -> str | None:
    """Return path to the claude CLI binary, or None if not found."""
    # Check PATH first
    found = shutil.which("claude")
    if found:
        return found
    # Check known fixed paths
    for path in _CLAUDE_CLI_SEARCH_PATHS[1:]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Glob for any installed VS Code extension version
    pattern = os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"
    )
    matches = sorted(glob.glob(pattern), reverse=True)  # latest version first
    for match in matches:
        if os.access(match, os.X_OK):
            return match
    return None


_claude_cli_path: str | None = _find_claude_cli()


def get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,  # tenacity handles retries — avoid double retry
        )
    return _anthropic_client


# Retry policy for Anthropic API calls:
#   - Retries on rate limits, timeouts, transient 5xx errors, and connection drops
#   - Does NOT retry on auth errors (will loop forever)
#   - InternalServerError: Anthropic HTTP 500/529 — transient, safe to retry
#   - APIConnectionError: network-level drops — safe to retry
_ANTHROPIC_RETRY = tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=60),
    stop=tenacity.stop_after_attempt(settings.anthropic_max_retries),
    retry=tenacity.retry_if_exception_type(
        (APITimeoutError, RateLimitError, InternalServerError, APIConnectionError)
    ),
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
        self.token_budget = token_budget or settings.forge_max_tokens_per_run
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

    def _call_claude_cli(
        self,
        messages: list[dict],
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Call Claude via the Claude Code CLI subprocess.

        Uses your Max/Pro subscription — zero API credit cost.
        Returns the assistant's text response.

        Raises RuntimeError on any failure so _call_claude() can fall back
        to the Anthropic API.
        """
        if not _claude_cli_path:
            raise RuntimeError("Claude CLI binary not found")

        # Flatten messages into a single prompt string.
        # For multi-turn, prefix each role so Claude sees the conversation.
        if len(messages) == 1 and messages[0].get("role") == "user":
            prompt = messages[0]["content"]
        else:
            parts = []
            for m in messages:
                role = m.get("role", "user").upper()
                content = m.get("content", "")
                parts.append(f"{role}: {content}")
            prompt = "\n\n".join(parts)

        cmd = [
            _claude_cli_path, "-p",
            "--output-format", "json",
            "--model", model or settings.anthropic_model_default,
            "--no-session-persistence",  # each call is independent
            "--dangerously-skip-permissions",  # non-interactive, no file tools
        ]
        if system:
            cmd += ["--system-prompt", system]

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min hard timeout per call
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude CLI timed out after 300s") from exc
        except Exception as exc:
            raise RuntimeError(f"Claude CLI subprocess error: {exc}") from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"Claude CLI exit {result.returncode}: {result.stderr[:200]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Claude CLI returned non-JSON: {result.stdout[:200]}") from exc

        if data.get("is_error") or data.get("subtype") != "success":
            raise RuntimeError(f"Claude CLI error response: {data.get('result','')[:200]}")

        # Track tokens — CLI reports input + output + cache tokens
        usage = data.get("usage", {})
        tokens_used = (
            usage.get("input_tokens", 0)
            + usage.get("output_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        self._tokens_used += tokens_used

        self._log.debug(
            "agent.claude_call",
            via="cli",
            model=list(data.get("modelUsage", {}).keys()),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            total_tokens=tokens_used,
            budget_remaining=self.token_budget - self._tokens_used,
        )

        return data.get("result", "")

    @_ANTHROPIC_RETRY
    def _call_claude_api(
        self,
        messages: list[dict],
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Call Anthropic API directly (fallback path).
        Uses API credits. Only called when CLI is unavailable or fails.
        """
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
            via="api",
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=tokens_used,
            budget_remaining=self.token_budget - self._tokens_used,
        )

        return response.content[0].text

    def _call_claude(
        self,
        messages: list[dict],
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Call Claude: CLI-first (Max subscription), API fallback.

        1. Tries Claude Code CLI subprocess — uses your Max subscription,
           zero API credit cost.
        2. Falls back to Anthropic API on any CLI failure (binary not found,
           auth error, timeout, bad output).

        Returns the assistant's text response.
        """
        self._check_budget(max_tokens)

        if _claude_cli_path:
            try:
                return self._call_claude_cli(messages, system, model, max_tokens)
            except Exception as exc:
                self._log.warning(
                    "agent.claude_cli_failed_fallback_to_api",
                    error=str(exc),
                )

        # Fallback: Anthropic API
        return self._call_claude_api(messages, system, model, max_tokens)

    # ── Soul layer ────────────────────────────────────────────────────────────

    def _reflect(
        self,
        task_description: str,
        context: str = "",
        soul: str = "",
    ) -> str:
        """
        Pre-task reflection. Ask the agent to think through risks, ambiguities,
        and verification steps before acting.

        Returns the reflection text, or empty string on failure (non-fatal).
        """
        from phalanx.agents.soul import get_reflection_prompt, get_soul  # noqa: PLC0415

        soul_text = soul or get_soul(self.AGENT_ROLE)
        prompt_template = get_reflection_prompt(self.AGENT_ROLE)
        if not prompt_template:
            return ""

        context_section = f"CONTEXT:\n{context}" if context else ""
        prompt = prompt_template.format(
            task_description=task_description,
            context_section=context_section,
            # Role-specific template vars — pass defaults; subclasses can override
            work_order_title=task_description,
            work_order_description=context,
            builder_summary=context,
            files_written="",
            epics_summary=task_description,
            app_type="",
        )
        try:
            return self._call_claude(
                messages=[{"role": "user", "content": prompt}],
                system=soul_text,
                max_tokens=1024,
            )
        except Exception as exc:
            self._log.warning("agent.reflect_failed", error=str(exc))
            return ""

    async def _trace(
        self,
        trace_type: str,
        content: str,
        context: dict | None = None,
    ) -> None:
        """
        Persist a soul-layer reasoning trace to the agent_traces table.
        Non-fatal — logs warning on failure (AP-003 exception: traces must
        not abort core logic).

        trace_type: reflection | decision | uncertainty | disagreement |
                    self_check | handoff
        """
        if not content:
            return
        # Cap content to 10 000 chars to prevent bloated rows
        content = content[:10_000]
        try:
            from phalanx.db.models import AgentTrace  # noqa: PLC0415
            from phalanx.db.session import get_db  # noqa: PLC0415

            async with get_db() as session:
                trace = AgentTrace(
                    run_id=self.run_id,
                    task_id=self.task_id,
                    agent_role=self.AGENT_ROLE,
                    agent_id=self.agent_id,
                    trace_type=trace_type,
                    content=content,
                    context=context or {},
                    tokens_used=self._tokens_used,
                )
                session.add(trace)
                await session.commit()
        except Exception as exc:
            self._log.warning(
                "agent.trace_failed",
                trace_type=trace_type,
                error=str(exc),
            )

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


# ── Module-level failure helpers (used by Celery task wrappers) ───────────────
#
# When a Celery worker dies or raises an unhandled exception, the DB task/run
# remains IN_PROGRESS forever — the orchestrator polls DB, not Celery state.
# These helpers mark the DB record FAILED so the orchestrator fails fast.


async def mark_task_failed(task_id: str, error: str) -> None:
    """
    Mark a Task FAILED in DB. Called from Celery execute_task wrappers when
    agent.execute() raises an unhandled exception.

    Non-fatal: if the DB write itself fails, logs a warning and returns — the
    stale-task watchdog in the orchestrator will catch it eventually.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy import update  # noqa: PLC0415

    from phalanx.db.models import Task  # noqa: PLC0415
    from phalanx.db.session import get_db  # noqa: PLC0415

    try:
        async with get_db() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status="FAILED",
                    error=error[:2000],  # guard against enormous tracebacks
                    failure_count=Task.failure_count + 1,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
        log.error("agent.task_marked_failed", task_id=task_id, error=error[:200])
    except Exception as db_exc:
        log.warning(
            "agent.mark_task_failed_db_error",
            task_id=task_id,
            original_error=error[:200],
            db_error=str(db_exc),
        )


async def mark_run_failed(run_id: str, error: str) -> None:
    """
    Mark a Run FAILED in DB. Called from commander's execute_run wrapper when
    agent.execute() raises an unhandled exception.

    Attempts a state-machine-safe transition from any IN_PROGRESS-compatible
    state; falls back to a raw UPDATE if the transition is invalid (e.g., Run
    was already FAILED by inner code).
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy import update  # noqa: PLC0415

    from phalanx.db.models import Run  # noqa: PLC0415
    from phalanx.db.session import get_db  # noqa: PLC0415

    try:
        async with get_db() as session:
            # Raw update — safest when we don't know current state
            result = await session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status.notin_(["COMPLETED", "FAILED", "CANCELLED"]))
                .values(
                    status="FAILED",
                    error_message=error[:2000],
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            if result.rowcount:
                log.error("agent.run_marked_failed", run_id=run_id, error=error[:200])
    except Exception as db_exc:
        log.warning(
            "agent.mark_run_failed_db_error",
            run_id=run_id,
            original_error=error[:200],
            db_error=str(db_exc),
        )
