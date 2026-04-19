"""CIFixerV2Agent — main loop.

This file currently contains the loop *skeleton* only. Real LLM calls
(GPT-5.4 main + Sonnet 4.6 coder subagent) and real tool implementations
land in later Week-1 chunks (spec §6). The skeleton is structured so
tests can drive the loop end-to-end with a mocked LLM callable and a
registered fake tool — proving control flow (verification gate, turn
cap, escalation triggers) before any provider integration is wired.

Spec references:
  - §3 system prompt
  - §6 loop pseudocode
  - §8 telemetry
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from phalanx.ci_fixer_v2 import tools as tools_module
from phalanx.ci_fixer_v2.config import (
    MAX_MAIN_TURNS,
    EscalationReason,
    RunVerdict,
)
from phalanx.ci_fixer_v2.context import AgentContext

log = structlog.get_logger(__name__)


# ── Provider-agnostic LLM I/O ─────────────────────────────────────────────
@dataclass
class LLMToolUse:
    """One tool-use request emitted by the main-agent LLM on a turn."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized provider response.

    Both OpenAI (Responses API) and Anthropic (Messages API) tool-use
    responses are squashed into this shape at the provider-adapter
    boundary so the loop itself stays provider-agnostic.
    """

    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | ...
    text: str = ""
    tool_uses: list[LLMToolUse] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


LLMCallable = Callable[[list[dict[str, Any]]], Awaitable[LLMResponse]]
"""Seam for the provider call. Real impl wraps OpenAI Responses API
with system prompt + tool schemas bound once; the loop calls it with
the current message list. Tests inject a scripted fake."""


# ── Terminal verdicts ─────────────────────────────────────────────────────
@dataclass
class RunOutcome:
    """Terminal state of one v2 run — returned by `run_ci_fix_v2`."""

    verdict: RunVerdict
    escalation_reason: EscalationReason | None = None
    committed_sha: str | None = None
    committed_branch: str | None = None
    explanation: str = ""


# ── Main loop ─────────────────────────────────────────────────────────────
async def run_ci_fix_v2(
    context: AgentContext,
    llm_call: LLMCallable,
    max_turns: int = MAX_MAIN_TURNS,
) -> RunOutcome:
    """Execute the v2 main loop until commit / escalation / turn cap.

    The loop itself is provider-agnostic and tool-agnostic — it only
    knows about the context, the LLM callable, and the tool registry.
    Tools register themselves at import time; the loop looks them up by
    name.

    Invariants (spec §6):
      - `commit_and_push` is never executed if `context.last_sandbox_verified`
        is False at call time → escalate VERIFICATION_GATE_VIOLATION.
      - A response with `stop_reason == "end_turn"` and no commit/escalate
        → escalate IMPLICIT_STOP.
      - Reaching `max_turns` without commit/escalate → auto-escalate
        TURN_CAP_REACHED.
    """
    logger = log.bind(
        ci_fix_run_id=context.ci_fix_run_id,
        repo=context.repo_full_name,
    )

    for turn in range(max_turns):
        response = await llm_call(context.messages)
        context.cost.gpt_reasoning_input_tokens += response.input_tokens
        context.cost.gpt_reasoning_output_tokens += response.output_tokens
        context.cost.gpt_reasoning_thinking_tokens += response.thinking_tokens

        if response.stop_reason == "end_turn" and not response.tool_uses:
            # Agent signaled done without commit or escalate — treat as
            # implicit escalation; we never commit silently.
            logger.info("v2.loop.implicit_stop", turn=turn, text=response.text[:200])
            return RunOutcome(
                verdict=RunVerdict.ESCALATED,
                escalation_reason=EscalationReason.IMPLICIT_STOP,
                explanation=response.text,
            )

        # Record the assistant turn in conversation history for the next LLM call.
        context.messages.append(
            {
                "role": "assistant",
                "content": _assistant_message_content(response),
            }
        )

        terminal = await _execute_tool_uses(context, response.tool_uses, turn, logger)
        if terminal is not None:
            return terminal

    # Loop exited without commit/escalate → auto-escalate.
    logger.warning("v2.loop.turn_cap_reached", max_turns=max_turns)
    return RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.TURN_CAP_REACHED,
        explanation=(
            f"Reached turn cap ({max_turns}) without a verified fix. "
            "See decision timeline on CIFixRun for the attempted path."
        ),
    )


async def _execute_tool_uses(
    context: AgentContext,
    tool_uses: list[LLMToolUse],
    turn: int,
    logger: Any,
) -> RunOutcome | None:
    """Dispatch every tool_use in this turn. Returns a terminal RunOutcome
    iff commit or escalate fires, else None (loop continues)."""
    for use in tool_uses:
        # Hard gates live here — not inside the tool, so a bug in the
        # tool can't bypass them.
        if use.name == "commit_and_push" and not context.last_sandbox_verified:
            logger.error(
                "v2.loop.verification_gate_violation",
                turn=turn,
                attempted_input=use.input,
            )
            return RunOutcome(
                verdict=RunVerdict.ESCALATED,
                escalation_reason=EscalationReason.VERIFICATION_GATE_VIOLATION,
                explanation=(
                    "commit_and_push called without sandbox verification. "
                    "This is a hard gate — the agent must re-run the "
                    "original failing command in sandbox and see it pass."
                ),
            )

        if not tools_module.base.is_registered(use.name):
            # Unknown tool — surface as a tool-result error so the agent
            # can recover on the next turn instead of crashing the run.
            context.messages.append(
                _tool_error_message(
                    use.id,
                    f"tool_not_registered: {use.name}",
                )
            )
            continue

        tool = tools_module.base.get(use.name)
        result = await tool.handler(context, use.input)

        context.tool_invocations.append(
            _record_tool_invocation(context, turn, use, result)
        )
        context.messages.append(_tool_result_message(use.id, result))

        # Terminal tools: escalate returns now; commit_and_push returns now.
        if use.name == "escalate":
            reason_str = use.input.get("reason", EscalationReason.LOW_CONFIDENCE.value)
            try:
                reason = EscalationReason(reason_str)
            except ValueError:
                reason = EscalationReason.LOW_CONFIDENCE
            return RunOutcome(
                verdict=RunVerdict.ESCALATED,
                escalation_reason=reason,
                explanation=use.input.get("explanation", ""),
            )

        if use.name == "commit_and_push" and result.ok:
            return RunOutcome(
                verdict=RunVerdict.COMMITTED,
                committed_sha=result.data.get("sha"),
                committed_branch=result.data.get("branch"),
                explanation="committed + pushed after sandbox verification",
            )

    return None


def _assistant_message_content(response: LLMResponse) -> list[dict[str, Any]]:
    """Build the content blocks for the assistant message appended after
    an LLM turn. Kept minimal at skeleton stage; provider adapters refine
    this when real LLM wiring lands."""
    blocks: list[dict[str, Any]] = []
    if response.text:
        blocks.append({"type": "text", "text": response.text})
    for use in response.tool_uses:
        blocks.append(
            {
                "type": "tool_use",
                "id": use.id,
                "name": use.name,
                "input": use.input,
            }
        )
    return blocks


def _tool_result_message(tool_use_id: str, result: Any) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result.to_tool_message_content(),
            }
        ],
    }


def _tool_error_message(tool_use_id: str, error: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "is_error": True,
                "content": {"ok": False, "error": error},
            }
        ],
    }


def _record_tool_invocation(
    context: AgentContext,
    turn: int,
    use: LLMToolUse,
    result: Any,
) -> Any:
    from phalanx.ci_fixer_v2.context import ToolInvocation

    return ToolInvocation(
        turn=turn,
        tool_name=use.name,
        tool_input=use.input,
        tool_result=result.data if result.ok else None,
        error=None if result.ok else result.error,
    )
