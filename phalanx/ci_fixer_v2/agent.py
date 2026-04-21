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

import asyncio
import time
import structlog
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from phalanx.ci_fixer_v2 import tools as tools_module
from phalanx.ci_fixer_v2.tools.base import ToolResult
from phalanx.ci_fixer_v2.config import (
    MAX_MAIN_TURNS,
    EscalationReason,
    RunVerdict,
)
from phalanx.ci_fixer_v2.context import AgentContext

log = structlog.get_logger(__name__)

_TOOL_DISPATCH_TIMEOUT_S: float = 300.0
"""Wall-clock cap on a single tool handler. Tools set their own internal
timeouts (run_in_sandbox: 120s, git apply: 60s, etc.), but a stuck
subprocess or network call can evade those — this outer asyncio.wait_for
is a hard floor that guarantees the agent loop never hangs on one tool
call. 300s is generous for legitimate work (sandbox tests can take 2+
min on slow repos) while still cutting off real hangs."""


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
        logger.info("v2.agent.turn_start", turn=turn, messages=len(context.messages))
        response = await llm_call(context.messages)
        context.cost.gpt_reasoning_input_tokens += response.input_tokens
        context.cost.gpt_reasoning_output_tokens += response.output_tokens
        context.cost.gpt_reasoning_thinking_tokens += response.thinking_tokens
        logger.info(
            "v2.agent.turn_response",
            turn=turn,
            stop_reason=response.stop_reason,
            tool_uses=[t.name for t in response.tool_uses],
            text_preview=response.text[:120] if response.text else "",
        )

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
        logger.info("v2.tool.start", turn=turn, tool=use.name)
        tool_started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                tool.handler(context, use.input), timeout=_TOOL_DISPATCH_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.error(
                "v2.tool.timeout",
                turn=turn,
                tool=use.name,
                elapsed_s=round(time.monotonic() - tool_started, 2),
                limit_s=_TOOL_DISPATCH_TIMEOUT_S,
            )
            result = ToolResult(
                ok=False,
                error=(
                    f"tool_timeout: {use.name} exceeded "
                    f"{_TOOL_DISPATCH_TIMEOUT_S}s wall-clock"
                ),
            )
        logger.info(
            "v2.tool.end",
            turn=turn,
            tool=use.name,
            ok=result.ok,
            elapsed_s=round(time.monotonic() - tool_started, 2),
        )

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
            # Evidence gate: some escalation reasons are load-bearing
            # for downstream triage (routing to infra vs. flagging a
            # broken main). If the LLM picks one of them without proof
            # in the tool trace, we coerce to LOW_CONFIDENCE — same
            # shape as the commit verification gate. The LLM still
            # escalates (its decision to stop trying is respected),
            # but the *reason* has to be backed by evidence.
            forced_reason = _force_low_confidence_if_no_evidence(context, reason)
            if forced_reason is not reason:
                logger.warning(
                    "v2.loop.escalation_reason_forced",
                    attempted_reason=reason.value,
                    forced_to=forced_reason.value,
                    turn=turn,
                )
                reason = forced_reason
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


# ─────────────────────────────────────────────────────────────────────
# Escalation evidence gate
# ─────────────────────────────────────────────────────────────────────
#
# Some escalation reasons are load-bearing — `infra_failure_out_of_scope`
# routes a run to the ops oncall, `preexisting_main_failure` tells a
# human that main itself is broken. If the LLM picks one of these
# without evidence, the routing is wrong and the on-call gets woken
# up for nothing. Instead of trusting the LLM's self-reported reason,
# we enforce at the LOOP level that certain reasons require concrete
# evidence in ctx.tool_invocations.
#
# When evidence is missing, we coerce to LOW_CONFIDENCE (the generic
# "I'm stuck" reason) and log `v2.loop.escalation_reason_forced` so
# the operator can see what the LLM *tried* to pick. The agent still
# escalates — we don't force the loop to keep running. Its decision
# to stop is respected; only the *reason* is rewritten.
#
# Same shape as the existing `verification_gate_violation` check on
# commit_and_push: invariants live in the loop, not in the prompt.
#
# To add a new gated reason, add an entry to ESCALATION_EVIDENCE_GATES
# with a function that returns True when sufficient evidence exists
# in ctx.tool_invocations.


def _has_infra_failure_evidence(context: AgentContext) -> bool:
    """True iff the tool trace contains at least one signal that the
    environment (not the agent's judgment) blocked progress.

    Evidence shapes:
      - a fetch_ci_log call that errored out (tool returned ok=False);
      - a run_in_sandbox call that exited 127 (command not found —
        sandbox missing a tool the CI has);
      - a run_in_sandbox call that timed out (per _exec_argv: exit 124
        with timed_out=True in the result payload).
    """
    for inv in context.tool_invocations:
        if inv.tool_name == "fetch_ci_log" and inv.error:
            return True
        if inv.tool_name == "run_in_sandbox":
            data = inv.tool_result or {}
            if data.get("exit_code") == 127:
                return True
            if data.get("timed_out") is True:
                return True
    return False


def _has_preexisting_main_failure_evidence(context: AgentContext) -> bool:
    """True iff the tool trace shows get_ci_history returned failing
    runs on the default branch. Matches on `branch in {main, master,
    default}` AND `conclusion == failure`.

    Shape of get_ci_history.data is implementation-specific; we
    defensively check the common forms."""
    for inv in context.tool_invocations:
        if inv.tool_name != "get_ci_history":
            continue
        data = inv.tool_result or {}
        # Preferred shape: aggregate counter
        if data.get("recent_main_failures", 0) > 0:
            return True
        # Fallback: enumerate runs
        for run in data.get("runs", []) or data.get("history", []):
            branch = (run.get("branch") or "").lower()
            concl = (run.get("conclusion") or "").lower()
            if branch in ("main", "master", "default") and concl == "failure":
                return True
    return False


# Reason → evidence-checker. A reason NOT in this map has no evidence
# requirement and will pass through unchanged. This is the whitelist
# style: we explicitly enumerate which reasons are load-bearing.
ESCALATION_EVIDENCE_GATES: dict[
    EscalationReason, Callable[[AgentContext], bool]
] = {
    EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE: _has_infra_failure_evidence,
    EscalationReason.PREEXISTING_MAIN_FAILURE: _has_preexisting_main_failure_evidence,
}


def _force_low_confidence_if_no_evidence(
    context: AgentContext, reason: EscalationReason
) -> EscalationReason:
    """Return the input reason unchanged iff either (a) it has no
    evidence gate, or (b) its gate finds evidence in the tool trace.
    Otherwise, return LOW_CONFIDENCE."""
    checker = ESCALATION_EVIDENCE_GATES.get(reason)
    if checker is None:
        return reason
    if checker(context):
        return reason
    return EscalationReason.LOW_CONFIDENCE
